"""SQLite storage: channels, their video catalogue, and searchable transcripts."""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    title         TEXT,
    handle        TEXT,
    avatar        TEXT,
    subscribers   INTEGER,
    status        TEXT NOT NULL DEFAULT 'scanning',   -- scanning | active | paused | error
    error         TEXT,
    include_shorts  INTEGER NOT NULL DEFAULT 0,
    include_streams INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    scanned_at    REAL
);

-- A video can appear in several catalogues (e.g. a channel and one of its
-- playlists); its status and transcript are shared between them.
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT NOT NULL,
    channel_id    TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    title         TEXT,
    kind          TEXT NOT NULL DEFAULT 'video',      -- video | short | stream
    position      INTEGER,                            -- order on the channel (0 = newest)
    duration      INTEGER,
    view_count    INTEGER,
    upload_date   TEXT,                               -- YYYY-MM-DD
    description   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',    -- pending | working | done | no_transcript | error
    source        TEXT,                               -- manual | auto | whisper
    language      TEXT,
    word_count    INTEGER,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL,
    PRIMARY KEY (channel_id, id)
);
CREATE INDEX IF NOT EXISTS idx_videos_id ON videos(id);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id, position);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

CREATE TABLE IF NOT EXISTS transcripts (
    video_id   TEXT PRIMARY KEY,
    segments   TEXT NOT NULL,   -- JSON list of {start, duration, text}
    text       TEXT NOT NULL
);

-- Transcript split into ~30s chunks so search hits can link to a timestamp.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, video_id UNINDEXED, start UNINDEXED,
    tokenize = 'porter unicode61'
);
"""

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            conn.executescript(SCHEMA)
            # Anything mid-flight when the server stopped goes back in the queue.
            conn.execute("UPDATE videos SET status = 'pending' WHERE status = 'working'")
            conn.execute("UPDATE channels SET status = 'error', error = 'Scan interrupted - press Rescan' "
                         "WHERE status = 'scanning'")
        _initialized = True


@contextmanager
def conn():
    init()
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def rows(sql: str, params=()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def row(sql: str, params=()) -> dict | None:
    with conn() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params=()) -> int:
    with conn() as c:
        return c.execute(sql, params).rowcount


# --- channels -----------------------------------------------------------------

def create_channel(channel_id: str, url: str, include_shorts: bool, include_streams: bool) -> None:
    execute(
        "INSERT INTO channels (id, url, include_shorts, include_streams, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'scanning') "
        "ON CONFLICT(id) DO UPDATE SET status = 'scanning', error = NULL, "
        "include_shorts = excluded.include_shorts, include_streams = excluded.include_streams",
        (channel_id, url, int(include_shorts), int(include_streams), time.time()),
    )


def update_channel(channel_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE channels SET {cols} WHERE id = ?", (*fields.values(), channel_id))


def rename_channel(old_id: str, new_id: str) -> str:
    """Replace a placeholder channel id with the real one once it is known.

    Returns the id to use from now on. If the real channel already exists the
    placeholder is dropped and the existing channel is reused.
    """
    if old_id == new_id:
        return new_id
    with conn() as c:
        if c.execute("SELECT 1 FROM channels WHERE id = ?", (new_id,)).fetchone():
            c.execute("DELETE FROM channels WHERE id = ?", (old_id,))
            c.execute("UPDATE channels SET status = 'scanning', error = NULL WHERE id = ?", (new_id,))
        else:
            c.execute("UPDATE channels SET id = ? WHERE id = ?", (new_id, old_id))
    return new_id


STATUS_COUNTS = """
    SELECT COUNT(*) AS total,
           SUM(status = 'done') AS done,
           SUM(status = 'pending') AS pending,
           SUM(status = 'working') AS working,
           SUM(status = 'no_transcript') AS no_transcript,
           SUM(status = 'error') AS error,
           COALESCE(SUM(CASE WHEN status = 'done' THEN word_count END), 0) AS words,
           COALESCE(SUM(duration), 0) AS seconds
    FROM videos WHERE channel_id = ?
"""


def channel_with_stats(channel_id: str) -> dict | None:
    ch = row("SELECT * FROM channels WHERE id = ?", (channel_id,))
    if ch:
        stats = row(STATUS_COUNTS, (channel_id,))
        ch["stats"] = {k: (v or 0) for k, v in stats.items()}
    return ch


def list_channels() -> list[dict]:
    ids = [r["id"] for r in rows("SELECT id FROM channels ORDER BY created_at DESC")]
    return [c for c in (channel_with_stats(i) for i in ids) if c]


def delete_channel(channel_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        # Drop transcripts no other catalogue still uses.
        orphan = "SELECT video_id FROM transcripts WHERE video_id NOT IN (SELECT id FROM videos)"
        c.execute(f"DELETE FROM chunks_fts WHERE video_id IN ({orphan})")
        c.execute(f"DELETE FROM transcripts WHERE video_id IN ({orphan})")


# --- videos -------------------------------------------------------------------

def upsert_videos(channel_id: str, videos: list[dict]) -> int:
    """Add newly discovered videos; refresh metadata of known ones. Returns # new."""
    new = 0
    with conn() as c:
        for v in videos:
            cur = c.execute(
                "INSERT INTO videos (id, channel_id, title, kind, position, duration, view_count, upload_date, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(channel_id, id) DO NOTHING",
                (v["id"], channel_id, v.get("title"), v.get("kind", "video"), v.get("position"),
                 v.get("duration"), v.get("view_count"), v.get("upload_date"), time.time()),
            )
            if cur.rowcount:
                new += 1
                # Already handled through another catalogue? Reuse that result.
                c.execute(
                    "UPDATE videos SET (status, source, language, word_count, error, upload_date, description) = "
                    "(SELECT o.status, o.source, o.language, o.word_count, o.error, "
                    "        COALESCE(videos.upload_date, o.upload_date), o.description "
                    " FROM videos o WHERE o.id = videos.id AND o.channel_id != videos.channel_id "
                    " ORDER BY o.status = 'done' DESC LIMIT 1) "
                    "WHERE channel_id = ? AND id = ? AND EXISTS "
                    "(SELECT 1 FROM videos o WHERE o.id = videos.id AND o.channel_id != videos.channel_id "
                    " AND o.status != 'pending')",
                    (channel_id, v["id"]),
                )
            else:
                c.execute(
                    "UPDATE videos SET title = COALESCE(?, title), position = ?, "
                    "view_count = COALESCE(?, view_count), duration = COALESCE(?, duration) "
                    "WHERE channel_id = ? AND id = ?",
                    (v.get("title"), v.get("position"), v.get("view_count"), v.get("duration"), channel_id, v["id"]),
                )
    return new


SORTS = {
    "newest": "position ASC",
    "oldest": "position DESC",
    "views": "view_count DESC",
    "longest": "duration DESC",
    "title": "title COLLATE NOCASE ASC",
}


def list_videos(channel_id: str, status: str | None = None, q: str | None = None,
                sort: str = "newest", kind: str | None = None) -> list[dict]:
    sql = ("SELECT id, title, kind, position, duration, view_count, upload_date, status, source, "
           "language, word_count, error FROM videos WHERE channel_id = ?")
    params: list = [channel_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if q:
        sql += " AND title LIKE ?"
        params.append(f"%{q}%")
    sql += f" ORDER BY {SORTS.get(sort, SORTS['newest'])}"
    return rows(sql, params)


def get_video(video_id: str) -> dict | None:
    v = row("SELECT * FROM videos WHERE id = ? LIMIT 1", (video_id,))
    if v:
        t = row("SELECT segments FROM transcripts WHERE video_id = ?", (video_id,))
        v["segments"] = json.loads(t["segments"]) if t else []
    return v


def claim_next_video() -> dict | None:
    """Atomically take the next pending video from an active channel."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute(
            "SELECT v.id, v.channel_id FROM videos v JOIN channels ch ON ch.id = v.channel_id "
            "WHERE v.status = 'pending' AND ch.status IN ('active', 'scanning') "
            "ORDER BY ch.created_at ASC, v.position ASC LIMIT 1"
        ).fetchone()
        if not r:
            return None
        c.execute("UPDATE videos SET status = 'working', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                  (time.time(), r["id"]))
        return dict(r)


def update_video(video_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE videos SET {cols} WHERE id = ?", (*fields.values(), video_id))


def save_transcript(video_id: str, segments: list[dict], source: str, language: str) -> None:
    text = " ".join(s["text"].strip() for s in segments if s["text"].strip())
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO transcripts (video_id, segments, text) VALUES (?, ?, ?)",
                  (video_id, json.dumps(segments), text))
        c.execute("DELETE FROM chunks_fts WHERE video_id = ?", (video_id,))
        for start, chunk in chunk_segments(segments):
            c.execute("INSERT INTO chunks_fts (text, video_id, start) VALUES (?, ?, ?)", (chunk, video_id, start))
        c.execute(
            "UPDATE videos SET status = 'done', source = ?, language = ?, word_count = ?, error = NULL, "
            "updated_at = ? WHERE id = ?",
            (source, language, len(text.split()), time.time(), video_id),
        )


def chunk_segments(segments: list[dict], seconds: float = 30.0):
    buf: list[str] = []
    start = None
    for s in segments:
        if start is None:
            start = s["start"]
        buf.append(s["text"].strip())
        if s["start"] + s.get("duration", 0) - start >= seconds:
            yield start, " ".join(buf)
            buf, start = [], None
    if buf:
        yield start, " ".join(buf)


def requeue(channel_id: str, statuses: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(statuses))
    return execute(f"UPDATE videos SET status = 'pending', error = NULL WHERE channel_id = ? AND status IN ({marks})",
                   (channel_id, *statuses))


def search(q: str, channel_id: str | None = None, limit: int = 100) -> list[dict]:
    match = fts_query(q)
    if not match:
        return []
    sql = ("SELECT f.video_id, f.start, snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 24) AS snippet, "
           "v.title, v.channel_id, ch.title AS channel_title "
           "FROM chunks_fts f "
           "JOIN videos v ON v.rowid = (SELECT rowid FROM videos WHERE id = f.video_id "
           "                            AND (? IS NULL OR channel_id = ?) LIMIT 1) "
           "JOIN channels ch ON ch.id = v.channel_id "
           "WHERE chunks_fts MATCH ?")
    params: list = [channel_id, channel_id, match]
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return rows(sql, params)


def fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query. "quoted phrases" are kept together."""
    import re
    parts = re.findall(r'"([^"]+)"|(\S+)', q)
    terms = []
    for phrase, word in parts:
        t = (phrase or word).replace('"', "")
        if t:
            terms.append(f'"{t}"')
    return " ".join(terms)
