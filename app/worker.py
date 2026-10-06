"""Background jobs: scanning channels and transcribing their videos."""

import logging
import threading
import time
import uuid

from . import config, db, youtube

log = logging.getLogger("ytt.worker")


def provider():
    if config.DEMO:
        from . import demo
        return demo
    return youtube


# --- channel scanning ---------------------------------------------------------

def start_scan(url: str, include_shorts: bool, include_streams: bool, channel_id: str | None = None) -> str:
    """Register a channel and catalogue it in the background. Returns its (provisional) id."""
    channel_id = channel_id or f"new-{uuid.uuid4().hex[:12]}"
    db.create_channel(channel_id, url, include_shorts, include_streams)
    threading.Thread(target=_scan, args=(channel_id, url, include_shorts, include_streams), daemon=True).start()
    return channel_id


def _scan(channel_id: str, url: str, include_shorts: bool, include_streams: bool) -> None:
    try:
        info = provider().fetch_catalog(url, include_shorts, include_streams)
        channel_id = db.rename_channel(channel_id, info["id"])
        db.update_channel(channel_id, title=info.get("title"), handle=info.get("handle"),
                          avatar=info.get("avatar"), subscribers=info.get("subscribers"))
        new = db.upsert_videos(channel_id, info["videos"])
        db.update_channel(channel_id, status="active", scanned_at=time.time(), error=None)
        log.info("Scanned %s: %d videos (%d new)", info.get("title"), len(info["videos"]), new)
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI
        log.exception("Scan failed for %s", url)
        db.update_channel(channel_id, status="error", error=str(e)[:500])


# --- transcription workers ----------------------------------------------------

class Pool:
    def __init__(self, size: int):
        self.size = size
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.threads: list[threading.Thread] = []
        # When YouTube blocks us, every worker backs off until this time.
        self.blocked_until = 0.0
        self.blocked_reason: str | None = None

    def start(self) -> None:
        for i in range(self.size):
            t = threading.Thread(target=self._run, name=f"ytt-worker-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()

    def nudge(self) -> None:
        self.wake.set()

    def status(self) -> dict:
        remaining = max(0, self.blocked_until - time.time())
        return {"workers": self.size, "blocked_for": round(remaining),
                "blocked_reason": self.blocked_reason if remaining else None, "demo": config.DEMO}

    def _run(self) -> None:
        while not self.stop.is_set():
            wait = self.blocked_until - time.time()
            if wait > 0:
                self.stop.wait(min(wait, 5))
                continue
            job = db.claim_next_video()
            if not job:
                self.wake.wait(3)
                self.wake.clear()
                continue
            self.process(job["id"], job["channel_id"])
            self.stop.wait(config.REQUEST_DELAY)

    def process(self, video_id: str, channel_id: str) -> None:
        p = provider()
        try:
            if config.FETCH_METADATA:
                try:
                    meta = {k: v for k, v in p.fetch_metadata(video_id).items() if v is not None}
                    if meta:
                        db.update_video(video_id, **meta)
                except Exception as e:  # metadata is nice-to-have
                    log.warning("Metadata failed for %s: %s", video_id, e)
            segments, source, language = p.transcribe(video_id)
            if not segments:
                raise youtube.NoTranscript("Transcript was empty")
            db.save_transcript(video_id, segments, source, language)
        except youtube.NoTranscript as e:
            db.update_video(video_id, status="no_transcript", error=str(e)[:300])
        except youtube.Blocked as e:
            # Put it back and pause everyone; retrying immediately makes blocks worse.
            db.update_video(video_id, status="pending", error=None)
            self.blocked_until = time.time() + 600
            self.blocked_reason = f"YouTube is rate-limiting requests ({e}). Pausing 10 minutes."
            log.warning(self.blocked_reason)
        except Exception as e:  # noqa: BLE001
            log.exception("Transcription failed for %s", video_id)
            db.update_video(video_id, status="error", error=str(e)[:300])


pool = Pool(config.WORKERS)
