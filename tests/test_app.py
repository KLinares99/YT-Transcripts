import io
import time
import zipfile

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)
    from app import db, demo
    monkeypatch.setattr(db, "_initialized", False)
    monkeypatch.setattr(demo.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    from app.main import app
    from app.worker import Pool
    import app.main as main
    monkeypatch.setattr(main, "pool", Pool(2))
    import app.worker as worker
    monkeypatch.setattr(worker, "pool", main.pool)
    with TestClient(app) as c:
        yield c


def wait_for(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = fn()
        if r:
            return r
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.mark.parametrize("raw,expected", [
    ("@veritasium", "https://www.youtube.com/@veritasium"),
    ("https://www.youtube.com/@3blue1brown/videos", "https://www.youtube.com/@3blue1brown"),
    ("youtube.com/@3blue1brown?si=abc", "https://www.youtube.com/@3blue1brown"),
    ("https://m.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw/featured",
     "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw"),
    ("UCYO_jab_esuFRV4b17AJtAw", "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw"),
    ("https://www.youtube.com/c/Vsauce", "https://www.youtube.com/c/Vsauce"),
    ("https://www.youtube.com/user/minutephysics/videos", "https://www.youtube.com/user/minutephysics"),
    ("https://www.youtube.com/watch?v=abc&list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab",
     "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab"),
])
def test_normalize_url(raw, expected):
    from app.youtube import normalize_url
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "https://example.com/foo", "https://www.youtube.com/watch?v=abc"])
def test_normalize_url_rejects(raw):
    from app.youtube import normalize_url
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_fts_query_is_safe():
    from app.db import fts_query
    assert fts_query('neural "gradient descent" AND (x') == '"neural" "gradient descent" "AND" "(x"'
    assert fts_query('   ') == ""


def test_chunking():
    from app.db import chunk_segments
    segs = [{"start": i * 10.0, "duration": 10.0, "text": f"s{i}"} for i in range(7)]
    chunks = list(chunk_segments(segs, 30))
    assert [c[0] for c in chunks] == [0.0, 30.0, 60.0]
    assert chunks[0][1] == "s0 s1 s2"


def test_srt_render():
    from app.main import render
    v = {"id": "x", "title": "T", "segments": [{"start": 1.5, "duration": 2.25, "text": "hello"}]}
    assert render(v, "srt") == "1\n00:00:01,500 --> 00:00:03,750\nhello\n"
    assert render(v, "txt") == "hello\n"
    assert "[0:01](https://www.youtube.com/watch?v=x&t=1s)" in render(v, "md")


def test_end_to_end(client):
    assert client.post("/api/channels", json={"url": "not a link"}).status_code == 400

    r = client.post("/api/channels", json={"url": "https://www.youtube.com/@demo-science"})
    assert r.status_code == 201

    def finished():
        chans = client.get("/api/channels").json()
        if len(chans) == 1 and chans[0]["status"] == "active":
            s = chans[0]["stats"]
            if s["total"] and s["pending"] == 0 and s["working"] == 0:
                return chans[0]
    ch = wait_for(finished)
    assert ch["id"].startswith("UCdemo")  # placeholder id replaced with the real one
    assert ch["title"] == "Demo Science"
    s = ch["stats"]
    assert s["done"] + s["no_transcript"] == s["total"] and s["done"] > 0 and s["words"] > 0

    videos = client.get(f"/api/channels/{ch['id']}/videos?sort=views").json()
    assert len(videos) == s["total"]
    views = [v["view_count"] for v in videos]
    assert views == sorted(views, reverse=True)
    done = next(v for v in videos if v["status"] == "done")

    v = client.get(f"/api/videos/{done['id']}").json()
    assert v["segments"] and v["source"] in ("manual", "auto")

    hits = client.get("/api/search", params={"q": "intuition"}).json()
    assert hits and "<mark>" in hits[0]["snippet"] and "start" in hits[0]

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/channels/{ch['id']}/export?format=zip").content))
    names = z.namelist()
    assert any(n.endswith("catalog.csv") for n in names)
    assert sum(n.endswith(".md") for n in names) == s["done"]
    assert client.get(f"/api/channels/{ch['id']}/export?format=json").json()["videos"]
    assert client.get(f"/api/videos/{done['id']}/download?format=srt").text.startswith("1\n")

    # Re-adding the same channel rescans instead of duplicating it.
    client.post("/api/channels", json={"url": "@demo-science"})
    wait_for(lambda: client.get(f"/api/channels/{ch['id']}").json()["status"] == "active")
    assert len(client.get("/api/channels").json()) == 1

    assert client.post(f"/api/channels/{ch['id']}/pause").json()["ok"]
    assert client.get(f"/api/channels/{ch['id']}").json()["status"] == "paused"

    client.delete(f"/api/channels/{ch['id']}")
    assert client.get("/api/channels").json() == []
    assert client.get("/api/search", params={"q": "intuition"}).json() == []


class FakeTranscript:
    def __init__(self, lang, generated):
        self.language_code, self.is_generated = lang, generated

    def fetch(self):
        from types import SimpleNamespace as NS
        return NS(snippets=[NS(start=0.0, duration=1.5, text=f"{self.language_code}-{self.is_generated}")])


class FakeListing:
    def __init__(self, items):
        self.items = items

    def __iter__(self):
        return iter(self.items)

    def _find(self, langs, generated):
        from youtube_transcript_api._errors import NoTranscriptFound
        for lang in langs:
            for t in self.items:
                if t.language_code == lang and t.is_generated == generated:
                    return t
        raise NoTranscriptFound("vid", langs, None)

    def find_manually_created_transcript(self, langs):
        return self._find(langs, False)

    def find_generated_transcript(self, langs):
        return self._find(langs, True)


@pytest.mark.parametrize("items,expected", [
    ([FakeTranscript("en", True), FakeTranscript("en", False)], ("manual", "en")),
    ([FakeTranscript("en", True), FakeTranscript("de", False)], ("auto", "en")),
    ([FakeTranscript("es", True), FakeTranscript("de", False)], ("manual", "de")),
    ([FakeTranscript("es", True)], ("auto", "es")),
])
def test_caption_selection(monkeypatch, items, expected):
    from app import youtube
    from types import SimpleNamespace as NS
    monkeypatch.setattr(youtube, "_transcript_api", lambda: NS(list=lambda vid: FakeListing(items)))
    segs, source, lang = youtube.fetch_captions("abcdefghijk")
    assert (source, lang) == expected and segs[0]["start"] == 0.0


def test_caption_errors(monkeypatch):
    from app import youtube
    from types import SimpleNamespace as NS
    from youtube_transcript_api._errors import IpBlocked, TranscriptsDisabled

    def boom(exc):
        def list_(vid):
            raise exc("abcdefghijk")
        return lambda: NS(list=list_)

    monkeypatch.setattr(youtube, "_transcript_api", lambda: NS(list=lambda vid: FakeListing([])))
    with pytest.raises(youtube.NoTranscript):
        youtube.fetch_captions("abcdefghijk")
    monkeypatch.setattr(youtube, "_transcript_api", boom(TranscriptsDisabled))
    with pytest.raises(youtube.NoTranscript):
        youtube.fetch_captions("abcdefghijk")
    monkeypatch.setattr(youtube, "_transcript_api", boom(IpBlocked))
    with pytest.raises(youtube.Blocked):
        youtube.fetch_captions("abcdefghijk")


def test_video_shared_between_catalogues(client):
    def settled():
        chans = client.get("/api/channels").json()
        return len(chans) == 2 and all(c["status"] == "active" and c["stats"]["total"]
                                       and not c["stats"]["pending"] and not c["stats"]["working"]
                                       for c in chans) and chans

    # Demo channels reuse the same sample video ids, like a playlist overlapping its channel.
    client.post("/api/channels", json={"url": "@chan-one"})
    wait_for(lambda: client.get("/api/channels").json()[0]["stats"]["done"])
    client.post("/api/channels", json={"url": "@chan-two"})
    a, b = wait_for(settled)
    shared = {v["id"] for v in client.get(f"/api/channels/{a['id']}/videos").json()} & \
             {v["id"] for v in client.get(f"/api/channels/{b['id']}/videos").json()}
    assert shared
    vid = sorted(shared)[0]
    for ch in (a, b):
        hits = client.get("/api/search", params={"q": "basics", "channel_id": ch["id"]}).json()
        assert hits and all(h["channel_id"] == ch["id"] for h in hits)

    client.delete(f"/api/channels/{a['id']}")
    assert client.get(f"/api/videos/{vid}").json()["channel_id"] == b["id"]
    assert client.get(f"/api/channels/{b['id']}").json()["stats"]["total"] > 0
    client.delete(f"/api/channels/{b['id']}")
    assert client.get("/api/search", params={"q": "basics"}).json() == []


def test_password_gate(client, monkeypatch):
    import base64
    from app import config
    monkeypatch.setattr(config, "PASSWORD", "s3cret")
    assert client.get("/api/channels").status_code == 401
    assert client.get("/healthz").status_code == 200
    bad = base64.b64encode(b"me:nope").decode()
    good = base64.b64encode(b"anyone:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert client.get("/api/channels", headers={"Authorization": f"Basic {good}"}).status_code == 200
