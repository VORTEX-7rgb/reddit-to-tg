"""
Utilities: state persistence, caption building, log rotation, disk cleanup.
"""
from __future__ import annotations

import html
import json
import logging
import logging.handlers
import os
import random
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import Config, CaptionCfg
from .reddit_scraper import ClipCandidate
from .safety_filter import SafetyDecision


def setup_logging(cfg: Config) -> logging.Logger:
    """Configure rotating file + console logging."""
    log_path = Path(cfg.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.logging.file,
        maxBytes=cfg.logging.max_size_mb * 1024 * 1024,
        backupCount=cfg.logging.keep_backups,
    )
    file_handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.logging.level.upper(), logging.INFO))
    # Avoid duplicate handlers on re-init
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)
    return logging.getLogger("karmabot")


def load_state(state_file: Path) -> dict:
    """Load posted-IDs state. Returns empty state if file missing."""
    if not state_file.exists():
        return {"posted": [], "history": []}
    try:
        with state_file.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"posted": [], "history": []}


def save_state(state_file: Path, state: dict) -> None:
    """Atomically save state (write to temp, rename)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(state_file)


def record_post(
    state: dict,
    candidate: ClipCandidate,
    message_id: int,
    decision: SafetyDecision,
) -> dict:
    """Append a successful post to state. Returns updated state."""
    state["posted"].append(candidate.post_id)
    # Keep posted list bounded (50000 IDs = ~11 years of history at 12/day, guarantees zero duplicate posts)
    if len(state["posted"]) > 50000:
        state["posted"] = state["posted"][-50000:]
    state["history"].append({
        "post_id": candidate.post_id,
        "title": candidate.title,
        "subreddit": candidate.subreddit,
        "author": candidate.author,
        "score": candidate.score,
        "url": candidate.permalink,
        "video_url": candidate.video_url,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "tg_message_id": message_id,
        "safety_codes": decision.reason_codes,
    })
    # Keep detailed history bounded to last 5000 items
    if len(state["history"]) > 5000:
        state["history"] = state["history"][-5000:]
    return state


def posts_today(state: dict) -> int:
    """Count posts made in the last 24h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for entry in state.get("history", []):
        try:
            ts = datetime.fromisoformat(entry["posted_at"])
            if ts > cutoff:
                count += 1
        except (KeyError, ValueError):
            continue
    return count


def is_quiet_hour(cfg: Config, now: datetime | None = None) -> bool:
    """Return True if current UTC hour is in quiet_hours list."""
    if not cfg.schedule.quiet_hours_utc:
        return False
    now = now or datetime.now(timezone.utc)
    return now.hour in cfg.schedule.quiet_hours_utc


def clean_reddit_title(title: str) -> str:
    """
    Unescape HTML entities from Reddit API (e.g. &amp; -> &, &#39; -> ')
    and strip annoying clickbait/bracket tags like [OC], (Sound ON), etc.
    """
    if not title:
        return ""
    # 1. Unescape HTML entities from Reddit API raw output
    text = html.unescape(title)

    # 2. Strip bracketed noise / clickbait tags
    noise_patterns = [
        r"\[(OC|Sound ON|Sound On|sound on|1080p|720p|4K|x-post|xpost|cross-post|Request|NSFW|Spoilers?)\]",
        r"\((Sound ON|Sound On|sound on|1080p|720p|4K|OC)\)",
    ]
    for pat in noise_patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    # 3. Collapse multiple spaces and trim
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_caption(cfg: CaptionCfg, candidate: ClipCandidate, decision: SafetyDecision) -> str:
    """
    Build the Telegram caption from the template, applying:
      - Title cleaning & unescaping
      - Profanity censoring (if enabled)
      - HTML safety escaping for Telegram HTML parse_mode
      - Hashtag rotation (random selection from pool)
      - Channel footer
      - Length truncation
    """
    raw_title = decision.censored_title or candidate.title
    cleaned_title = clean_reddit_title(raw_title)

    # Escape HTML special characters so Telegram HTML parse_mode renders cleanly
    safe_title = html.escape(cleaned_title, quote=False)
    safe_author = html.escape(candidate.author, quote=False)
    safe_subreddit = html.escape(candidate.subreddit, quote=False)
    safe_url = html.escape(candidate.permalink, quote=False)

    # Pick N random hashtags
    pool = list(cfg.hashtag_pool)
    random.shuffle(pool)
    hashtags = " ".join(pool[:cfg.hashtags_per_post])

    text = cfg.template.format(
        title=safe_title,
        author=safe_author,
        subreddit=safe_subreddit,
        url=safe_url,
        hashtags=hashtags,
    ).strip()

    if cfg.channel_footer:
        text = f"{text}\n\n{cfg.channel_footer}"

    # Truncate to Telegram limit (1024) — leave headroom
    if len(text) > cfg.max_length:
        text = text[:cfg.max_length - 3] + "..."

    return text


def cleanup_old_media(media_dir: Path, retain_days: int, max_disk_mb: int) -> int:
    """Delete media files older than retain_days OR until under max_disk_mb.
    Returns number of files deleted."""
    if not media_dir.exists():
        return 0
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - (retain_days * 86400)

    files = []
    for p in media_dir.iterdir():
        if p.is_file() and p.suffix in (".mp4", ".webm", ".mkv", ".jpg", ".png"):
            files.append((p, p.stat().st_mtime, p.stat().st_size))

    deleted = 0
    # Pass 1: delete by age
    for p, mtime, _ in files:
        if mtime < cutoff:
            try:
                p.unlink()
                deleted += 1
            except OSError:
                pass

    # Pass 2: if still over disk limit, delete oldest first
    total_mb = sum(s for _, _, s in files) / (1024*1024)
    if total_mb > max_disk_mb:
        files.sort(key=lambda x: x[1])  # oldest first
        for p, _, size in files:
            if total_mb <= max_disk_mb:
                break
            try:
                p.unlink()
                total_mb -= size / (1024*1024)
                deleted += 1
            except OSError:
                pass

    return deleted


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100]
