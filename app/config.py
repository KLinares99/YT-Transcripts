import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.getenv("YTT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.getenv("YTT_DB_PATH", DATA_DIR / "transcripts.db"))

# Number of videos transcribed in parallel. Keep this low: YouTube rate-limits
# aggressive transcript fetching and may temporarily block your IP.
WORKERS = int(os.getenv("YTT_WORKERS", "2"))
# Pause (seconds) each worker takes between videos.
REQUEST_DELAY = float(os.getenv("YTT_REQUEST_DELAY", "1.0"))
# Preferred caption languages, in order. Anything else is used as a fallback.
LANGUAGES = [s.strip() for s in os.getenv("YTT_LANGUAGES", "en,en-US,en-GB").split(",") if s.strip()]
# Fetch per-video metadata (upload date, description) with yt-dlp. Slower but richer.
FETCH_METADATA = _bool("YTT_FETCH_METADATA", True)
# Fall back to local Whisper transcription when a video has no captions.
WHISPER_ENABLED = _bool("YTT_WHISPER", False)
WHISPER_MODEL = os.getenv("YTT_WHISPER_MODEL", "base")
# Optional HTTP(S) proxy for YouTube requests, e.g. http://user:pass@host:port
PROXY = os.getenv("YTT_PROXY") or None
# Demo mode: generate fake channels instead of contacting YouTube.
DEMO = _bool("YTT_DEMO", False)
