"""FastAPI app: REST API + the single-page UI."""

import csv
import io
import json
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, youtube
from .worker import pool, start_scan

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    pool.start()
    yield
    pool.shutdown()


app = FastAPI(title="YT Transcripts", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# --- channels -----------------------------------------------------------------

class AddChannel(BaseModel):
    url: str
    include_shorts: bool = False
    include_streams: bool = False


def _channel_or_404(channel_id: str) -> dict:
    ch = db.channel_with_stats(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    return ch


@app.get("/api/status")
def status():
    return pool.status()


@app.get("/api/channels")
def list_channels():
    return db.list_channels()


@app.post("/api/channels", status_code=201)
def add_channel(body: AddChannel):
    try:
        url = youtube.normalize_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    existing = db.row("SELECT id FROM channels WHERE url = ?", (url,))
    channel_id = start_scan(url, body.include_shorts, body.include_streams,
                            channel_id=existing["id"] if existing else None)
    pool.nudge()
    return {"id": channel_id, "url": url}


@app.get("/api/channels/{channel_id}")
def get_channel(channel_id: str):
    return _channel_or_404(channel_id)


@app.post("/api/channels/{channel_id}/rescan")
def rescan(channel_id: str):
    ch = _channel_or_404(channel_id)
    start_scan(ch["url"], bool(ch["include_shorts"]), bool(ch["include_streams"]), channel_id=channel_id)
    return {"ok": True}


@app.post("/api/channels/{channel_id}/pause")
def pause(channel_id: str):
    _channel_or_404(channel_id)
    db.update_channel(channel_id, status="paused")
    return {"ok": True}


@app.post("/api/channels/{channel_id}/resume")
def resume(channel_id: str):
    _channel_or_404(channel_id)
    db.update_channel(channel_id, status="active")
    pool.nudge()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/retry")
def retry(channel_id: str, include_missing: bool = False):
    _channel_or_404(channel_id)
    statuses = ("error", "no_transcript") if include_missing else ("error",)
    n = db.requeue(channel_id, statuses)
    pool.nudge()
    return {"requeued": n}


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: str):
    _channel_or_404(channel_id)
    db.delete_channel(channel_id)
    return {"ok": True}


@app.get("/api/channels/{channel_id}/videos")
def channel_videos(channel_id: str, status: str | None = None, q: str | None = None,
                   sort: str = "newest", kind: str | None = None):
    _channel_or_404(channel_id)
    return db.list_videos(channel_id, status or None, q or None, sort, kind or None)


# --- videos & search ----------------------------------------------------------

@app.get("/api/videos/{video_id}")
def get_video(video_id: str):
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    return v


@app.post("/api/videos/{video_id}/retry")
def retry_video(video_id: str):
    if not db.get_video(video_id):
        raise HTTPException(404, "Video not found")
    db.update_video(video_id, status="pending", error=None)
    pool.nudge()
    return {"ok": True}


@app.get("/api/search")
def search(q: str, channel_id: str | None = None):
    return db.search(q, channel_id or None)


# --- exports ------------------------------------------------------------------

def _ts(seconds: float, srt: bool = False) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")


def render(video: dict, fmt: str) -> str:
    segs = video["segments"]
    url = f"https://www.youtube.com/watch?v={video['id']}"
    if fmt == "txt":
        return " ".join(s["text"].strip() for s in segs) + "\n"
    if fmt == "srt":
        return "\n".join(f"{i}\n{_ts(s['start'], True)} --> {_ts(s['start'] + s['duration'], True)}\n{s['text']}\n"
                         for i, s in enumerate(segs, 1))
    # markdown, paragraphs of ~60s with clickable timestamps
    out = [f"# {video['title']}", "", f"- URL: {url}"]
    if video.get("upload_date"):
        out.append(f"- Published: {video['upload_date']}")
    out += [f"- Transcript: {video.get('source')} ({video.get('language')})", ""]
    para, start = [], None
    for s in segs:
        if start is None:
            start = s["start"]
        para.append(s["text"].strip())
        if s["start"] - start >= 60:
            out += [f"**[{_ts(start)}]({url}&t={int(start)}s)** " + " ".join(para), ""]
            para, start = [], None
    if para:
        out += [f"**[{_ts(start)}]({url}&t={int(start)}s)** " + " ".join(para), ""]
    return "\n".join(out)


def _slug(s: str) -> str:
    return re.sub(r"[^\w\-]+", "-", s or "untitled").strip("-")[:80] or "untitled"


@app.get("/api/videos/{video_id}/download")
def download_video(video_id: str, format: str = "md"):
    v = db.get_video(video_id)
    if not v or not v["segments"]:
        raise HTTPException(404, "No transcript for this video")
    fmt = format if format in ("md", "txt", "srt") else "md"
    return PlainTextResponse(render(v, fmt), headers={
        "Content-Disposition": f'attachment; filename="{_slug(v["title"])}.{fmt}"'})


CATALOG_FIELDS = ["id", "title", "kind", "upload_date", "duration", "view_count", "status", "source",
                  "language", "word_count", "url"]


@app.get("/api/channels/{channel_id}/export")
def export_channel(channel_id: str, format: str = "zip"):
    ch = _channel_or_404(channel_id)
    videos = db.list_videos(channel_id, sort="newest")
    name = _slug(ch.get("title") or channel_id)

    def catalog_csv() -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CATALOG_FIELDS, extrasaction="ignore")
        w.writeheader()
        for v in videos:
            w.writerow({**v, "url": f"https://www.youtube.com/watch?v={v['id']}"})
        return buf.getvalue()

    if format == "csv":
        return Response(catalog_csv(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}-catalog.csv"'})
    if format == "json":
        full = [db.get_video(v["id"]) for v in videos]
        data = {"channel": {k: ch[k] for k in ("id", "title", "handle", "url")}, "videos": full}
        return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}.json"'})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}/catalog.csv", catalog_csv())
        for v in videos:
            if v["status"] != "done":
                continue
            full = db.get_video(v["id"])
            prefix = f"{full.get('upload_date') or ''}_{_slug(full['title'])}_{full['id']}".lstrip("_")
            z.writestr(f"{name}/markdown/{prefix}.md", render(full, "md"))
            z.writestr(f"{name}/text/{prefix}.txt", render(full, "txt"))
            z.writestr(f"{name}/srt/{prefix}.srt", render(full, "srt"))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}-transcripts.zip"'})
