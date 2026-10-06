"""Everything that talks to YouTube: channel catalogues, metadata and transcripts."""

import re
import tempfile
from pathlib import Path

from . import config


class NoTranscript(Exception):
    """The video has no captions (and Whisper is disabled or failed)."""


class Blocked(Exception):
    """YouTube is refusing our requests (rate limited / IP blocked)."""


# --- URL handling -------------------------------------------------------------

_CHANNEL_PATTERNS = [
    r"youtube\.com/(@[\w.\-%]+)",
    r"youtube\.com/(channel/UC[\w-]{22})",
    r"youtube\.com/(c/[\w.\-%]+)",
    r"youtube\.com/(user/[\w.\-%]+)",
]


def normalize_url(raw: str) -> str:
    """Return a canonical channel or playlist URL (without a tab suffix)."""
    s = raw.strip()
    if not s:
        raise ValueError("Please paste a YouTube channel link")
    if s.startswith("@"):
        return f"https://www.youtube.com/{s.split('/')[0]}"
    if re.fullmatch(r"UC[\w-]{22}", s):
        return f"https://www.youtube.com/channel/{s}"
    if not s.startswith("http"):
        s = "https://" + s
    m = re.search(r"[?&]list=([\w-]+)", s)
    if m:
        return f"https://www.youtube.com/playlist?list={m.group(1)}"
    for pat in _CHANNEL_PATTERNS:
        m = re.search(pat, s)
        if m:
            return f"https://www.youtube.com/{m.group(1)}"
    raise ValueError("That doesn't look like a YouTube channel or playlist link")


# --- channel catalogue --------------------------------------------------------

def _ydl(extra: dict | None = None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": False}
    if config.PROXY:
        opts["proxy"] = config.PROXY
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _pick_avatar(thumbnails: list[dict] | None) -> str | None:
    if not thumbnails:
        return None
    for t in thumbnails:
        if t.get("id") in ("avatar_uncropped", "avatar"):
            return t.get("url")
    square = [t for t in thumbnails if t.get("width") and t.get("width") == t.get("height")]
    return (square or thumbnails)[-1].get("url")


def _fmt_date(d: str | None) -> str | None:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 else None


def fetch_catalog(url: str, include_shorts: bool = False, include_streams: bool = False) -> dict:
    """List every video on a channel (or playlist).

    Returns {"id", "title", "handle", "avatar", "subscribers", "videos": [...]}
    """
    tabs = [("videos", "video")]
    if include_shorts:
        tabs.append(("shorts", "short"))
    if include_streams:
        tabs.append(("streams", "stream"))
    is_playlist = "playlist?list=" in url
    if is_playlist:
        tabs = [(None, "video")]

    info_out: dict = {}
    videos: list[dict] = []
    seen: set[str] = set()
    errors = []
    with _ydl({"extract_flat": "in_playlist"}) as ydl:
        for tab, kind in tabs:
            tab_url = url if tab is None else f"{url}/{tab}"
            try:
                info = ydl.extract_info(tab_url, download=False)
            except Exception as e:  # tab may not exist (e.g. channel has no shorts)
                errors.append(str(e))
                continue
            if not info_out:
                info_out = {
                    "id": info.get("channel_id") or info.get("id"),
                    "title": info.get("channel") or info.get("uploader") or info.get("title"),
                    "handle": info.get("uploader_id"),
                    "avatar": _pick_avatar(info.get("thumbnails")),
                    "subscribers": info.get("channel_follower_count"),
                }
                if is_playlist:
                    info_out["id"] = info.get("id")
                    info_out["title"] = info.get("title")
            for e in info.get("entries") or []:
                if not e or not e.get("id") or e["id"] in seen or len(e["id"]) != 11:
                    continue
                seen.add(e["id"])
                videos.append({
                    "id": e["id"],
                    "title": e.get("title"),
                    "kind": kind,
                    "duration": int(e["duration"]) if e.get("duration") else None,
                    "view_count": e.get("view_count"),
                    "upload_date": _fmt_date(e.get("upload_date")),
                })
    if not info_out:
        raise RuntimeError(errors[0] if errors else "Could not read that channel")
    for i, v in enumerate(videos):
        v["position"] = i
    info_out["videos"] = videos
    return info_out


def fetch_metadata(video_id: str) -> dict:
    with _ydl() as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    return {
        "upload_date": _fmt_date(info.get("upload_date")),
        "description": info.get("description"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
    }


# --- transcripts --------------------------------------------------------------

def _transcript_api():
    from youtube_transcript_api import YouTubeTranscriptApi
    if config.PROXY:
        from youtube_transcript_api.proxies import GenericProxyConfig
        return YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=config.PROXY, https_url=config.PROXY))
    return YouTubeTranscriptApi()


def fetch_captions(video_id: str) -> tuple[list[dict], str, str]:
    """Return (segments, source, language). source is 'manual' or 'auto'."""
    from youtube_transcript_api import _errors as yta_errors

    blocked = tuple(getattr(yta_errors, n) for n in ("RequestBlocked", "IpBlocked", "TooManyRequests")
                    if hasattr(yta_errors, n))
    try:
        listing = _transcript_api().list(video_id)
        available = list(listing)
        transcript = None
        for finder in (listing.find_manually_created_transcript, listing.find_generated_transcript):
            try:
                transcript = finder(config.LANGUAGES)
                break
            except yta_errors.NoTranscriptFound:
                pass
        if transcript is None:
            if not available:
                raise NoTranscript("No captions available")
            # Prefer human captions in any language, then auto-generated.
            available.sort(key=lambda t: t.is_generated)
            transcript = available[0]
        fetched = transcript.fetch()
    except blocked as e:
        raise Blocked(str(e).strip().splitlines()[0]) from e
    except (yta_errors.TranscriptsDisabled, yta_errors.NoTranscriptFound) as e:
        raise NoTranscript("Captions are disabled for this video") from e
    except yta_errors.CouldNotRetrieveTranscript as e:
        raise NoTranscript(str(e).strip().splitlines()[0]) from e

    segments = [{"start": round(s.start, 2), "duration": round(s.duration, 2), "text": s.text}
                for s in fetched.snippets]
    return segments, ("auto" if transcript.is_generated else "manual"), transcript.language_code


_whisper_model = None


def transcribe_with_whisper(video_id: str) -> tuple[list[dict], str, str]:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise NoTranscript("No captions, and faster-whisper is not installed") from e
    if _whisper_model is None:
        _whisper_model = WhisperModel(config.WHISPER_MODEL, compute_type="int8")
    with tempfile.TemporaryDirectory() as tmp:
        with _ydl({"skip_download": False, "format": "bestaudio/best",
                   "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s")}) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        audio = next(Path(tmp).iterdir())
        segs, info = _whisper_model.transcribe(str(audio), vad_filter=True)
        segments = [{"start": round(s.start, 2), "duration": round(s.end - s.start, 2), "text": s.text.strip()}
                    for s in segs]
    return segments, "whisper", info.language


def transcribe(video_id: str) -> tuple[list[dict], str, str]:
    try:
        return fetch_captions(video_id)
    except NoTranscript:
        if config.WHISPER_ENABLED:
            return transcribe_with_whisper(video_id)
        raise
