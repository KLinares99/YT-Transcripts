"""Fake YouTube provider used when YTT_DEMO=1 (try the UI without network access)."""

import hashlib
import random
import time

from .youtube import NoTranscript

TOPICS = ["linear algebra", "neural networks", "compound interest", "sourdough", "black holes",
          "chess openings", "Rust ownership", "the French Revolution", "home espresso", "marathon training",
          "prime numbers", "photosynthesis", "jazz harmony", "SQL indexes", "the immune system"]
FORMATS = ["{} explained in 10 minutes", "The surprising truth about {}", "Why {} matters",
           "A beginner's guide to {}", "{}: everything you need to know", "I tried {} for 30 days",
           "The history of {}", "5 mistakes people make with {}"]
SENTENCES = [
    "So today we're going to dig into {t}.", "Let's start with the basics of {t}.",
    "A lot of people get this part of {t} wrong.", "Here's the key idea I want you to remember.",
    "If you think about it visually, it starts to click.", "Now let's look at a concrete example.",
    "This is where it gets really interesting.", "Pause for a second and try to predict what happens.",
    "The answer surprised me the first time I saw it.", "And that's the core intuition behind {t}.",
    "Let me draw this out on the board.", "Notice how everything lines up here.",
    "We'll come back to this in a later video.", "Thanks for watching, and I'll see you next time.",
]


def _rng(seed: str) -> random.Random:
    return random.Random(int(hashlib.md5(seed.encode()).hexdigest(), 16))


# Real, publicly embeddable video ids so thumbnails and the player work in demo mode.
SAMPLE_IDS = ["aircAruvnKk", "IHZwWFHWa-w", "Ilg3gGewQ5U", "tIeHLnjs5U8", "wjZofJX0v4M", "eMlx5fFNoYc",
              "9-Jl0dxWQs8", "fNk_zzaMoSs", "k7RM-ot2NWY", "PFDu9oVAE-g", "XkY2DOUCWMU", "rHLEWRxRGiM",
              "kYB8IZa5AuE", "LyGKycYT2v0", "v8VSDg_WQlA", "BaM7OCEm3G0", "TgKwz5Ikpc8", "uQhTuRlWMxw"]


def fetch_catalog(url: str, include_shorts: bool = False, include_streams: bool = False) -> dict:
    time.sleep(1.0)
    r = _rng(url)
    name = url.rstrip("/").split("/")[-1].lstrip("@") or "demo"
    n = r.randint(12, 18)
    videos = []
    for i in range(n):
        topic = r.choice(TOPICS)
        videos.append({
            "id": SAMPLE_IDS[i % len(SAMPLE_IDS)] if i < len(SAMPLE_IDS) else f"demo{i:07d}",
            "title": r.choice(FORMATS).format(topic).capitalize(),
            "kind": "video",
            "position": i,
            "duration": r.randint(180, 2400),
            "view_count": int(r.paretovariate(1.2) * 20000),
            "upload_date": f"20{25 - i // 6:02d}-{12 - i % 12:02d}-{r.randint(1, 28):02d}",
        })
    return {"id": "UCdemo" + hashlib.md5(url.encode()).hexdigest()[:18], "title": name.replace("-", " ").title(),
            "handle": "@" + name, "avatar": None, "subscribers": r.randint(10_000, 3_000_000), "videos": videos}


def fetch_metadata(video_id: str) -> dict:
    return {"description": "Demo description for this video."}


def transcribe(video_id: str):
    time.sleep(_rng(video_id).uniform(0.8, 2.0))
    r = _rng(video_id + "t")
    if r.random() < 0.08:
        raise NoTranscript("Captions are disabled for this video")
    topic = r.choice(TOPICS)
    t, segments = 0.0, []
    for _ in range(r.randint(60, 140)):
        d = round(r.uniform(2.0, 5.5), 2)
        segments.append({"start": round(t, 2), "duration": d, "text": r.choice(SENTENCES).format(t=topic)})
        t += d
    return segments, r.choice(["manual", "auto", "auto"]), "en"
