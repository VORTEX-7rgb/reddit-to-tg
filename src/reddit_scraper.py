"""
Reddit scraper via PRAW (official Reddit API — safest, no shadowbans).
Pulls top posts from configured subreddits, filters to video-hosted only,
and returns a normalized list of clip candidates.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Iterator

import praw
import requests
from praw.models import Submission

from .config import Config, RedditCfg

log = logging.getLogger(__name__)


@dataclass
class ClipCandidate:
    """A single Reddit post that's a candidate for posting to Telegram."""
    post_id: str            # Reddit submission id (e.g. "1abc23")
    title: str
    author: str             # u/username (or "deleted")
    subreddit: str          # r/subname (no 'r/' prefix)
    permalink: str          # full https URL
    score: int              # upvotes
    created_utc: float
    url: str                # direct media URL or reddit post URL
    is_video: bool          # True if reddit-hosted video
    is_gallery: bool
    is_nsfw: bool
    is_quarantined: bool
    video_url: str | None   # direct mp4 URL (None if not extractable here)
    thumbnail_url: str | None
    duration_sec: float | None
    width: int | None
    height: int | None
    over_18: bool

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc).timestamp() - self.created_utc) / 3600.0


class RedditScraper:
    """Scrapes Reddit via PRAW (if API credentials provided) or public HTTP JSON endpoints (if no credentials)."""

    def __init__(self, cfg: RedditCfg):
        self.cfg = cfg
        self.use_praw = bool(cfg.client_id and cfg.client_secret)
        self.reddit = None
        if self.use_praw:
            try:
                self.reddit = praw.Reddit(
                    client_id=cfg.client_id,
                    client_secret=cfg.client_secret,
                    user_agent=cfg.user_agent,
                    check_for_async=False,
                )
                self.reddit.read_only = True
                log.info("Reddit API authenticated via PRAW as read_only.")
            except Exception as e:
                log.warning("PRAW auth failed (%s), falling back to public HTTP JSON endpoints.", e)
                self.use_praw = False

        if not self.use_praw:
            log.info("Using public Reddit HTTP JSON endpoints (no API credentials required).")

    def iter_candidates(self, already_posted: set[str]) -> Iterator[ClipCandidate]:
        """
        Pull posts from all configured subreddits. Yields ClipCandidate objects
        that pass basic filters. Skips posts already in `already_posted`.
        """
        if self.use_praw:
            yield from self._iter_candidates_praw(already_posted)
        else:
            yield from self._iter_candidates_http(already_posted)

    def _iter_candidates_praw(self, already_posted: set[str]) -> Iterator[ClipCandidate]:
        sub_str = "+".join(self.cfg.subreddits)
        log.info("Scraping r/%s via PRAW (top, past 24h)...", sub_str)
        subreddit = self.reddit.subreddit(sub_str)
        limit = max(50, len(self.cfg.subreddits) * 25)
        for post in subreddit.top(time_filter="day", limit=limit):
            if post.id in already_posted:
                continue
            candidate = self._post_to_candidate(post)
            if candidate is None or not self._passes_filters(candidate):
                continue
            yield candidate

    def _iter_candidates_http(self, already_posted: set[str]) -> Iterator[ClipCandidate]:
        sub_str = "+".join(self.cfg.subreddits)
        url = f"https://www.reddit.com/r/{sub_str}/top.json?t=day&limit=100"
        headers = {
            "User-Agent": self.cfg.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        log.info("Scraping public HTTP JSON from %s ...", url)
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            items = data.get("data", {}).get("children", [])
            fetched = len(items)
            yielded = 0
            for item in items:
                p = item.get("data", {})
                post_id = p.get("id")
                if not post_id or post_id in already_posted:
                    continue
                candidate = self._json_dict_to_candidate(p)
                if candidate is None or not self._passes_filters(candidate):
                    continue
                yielded += 1
                yield candidate
            log.info("Fetched %d posts from Reddit HTTP JSON, yielded %d candidates.", fetched, yielded)
        except Exception as e:
            log.error("HTTP JSON scraper failed: %s", e)

    def _json_dict_to_candidate(self, p: dict) -> ClipCandidate | None:
        """Convert a Reddit JSON dict object to a ClipCandidate."""
        if p.get("is_self"):
            return None

        is_video = bool(p.get("is_video", False))
        is_gallery = bool(p.get("is_gallery", False))
        url = p.get("url", "")
        media = p.get("media") or {}

        video_url = None
        duration = None
        width = None
        height = None

        if is_video and isinstance(media, dict) and "reddit_video" in media:
            rv = media["reddit_video"]
            video_url = rv.get("fallback_url")
            duration = rv.get("duration")
            width = rv.get("width")
            height = rv.get("height")
        elif url and any(host in url for host in (
            "gfycat.com", "redgifs.com", "imgur.com", "streamable.com",
            "youtube.com", "youtu.be", "v.redd.it"
        )):
            video_url = url
            if "v.redd.it" in url:
                is_video = True
        elif url and (url.endswith(".mp4") or url.endswith(".gifv")):
            video_url = url.replace(".gifv", ".mp4")
            is_video = True
        else:
            return None

        if is_gallery and not is_video:
            return None

        author = p.get("author") or "deleted"
        permalink = p.get("permalink", "")

        return ClipCandidate(
            post_id=p["id"],
            title=str(p.get("title", "")).strip(),
            author=author,
            subreddit=str(p.get("subreddit", "")),
            permalink=f"https://reddit.com{permalink}",
            score=int(p.get("score", 0)),
            created_utc=float(p.get("created_utc", 0)),
            url=url,
            is_video=is_video,
            is_gallery=is_gallery,
            is_nsfw=bool(p.get("over_18", False)),
            is_quarantined=bool(p.get("quarantine", False)),
            video_url=video_url,
            thumbnail_url=p.get("thumbnail") if p.get("thumbnail") not in ("", "self", "default") else None,
            duration_sec=float(duration) if duration else None,
            width=int(width) if width else None,
            height=int(height) if height else None,
            over_18=bool(p.get("over_18", False)),
        )

    def _post_to_candidate(self, post: Submission) -> ClipCandidate | None:
        """Convert a PRAW Submission to a ClipCandidate, or None if not a video."""
        # Skip non-video, non-link posts (text posts, etc.)
        if post.is_self:
            return None

        is_video = bool(getattr(post, "is_video", False))
        is_gallery = bool(getattr(post, "is_gallery", False))

        video_url = None
        duration = None
        width = post.media_metadata.get('width') if post.media_metadata else None
        height = post.media_metadata.get('height') if post.media_metadata else None

        # Reddit-hosted video: media.reddit_video.dash_url or fallback_url
        if is_video and post.media and "reddit_video" in post.media:
            rv = post.media["reddit_video"]
            video_url = rv.get("fallback_url")  # direct mp4
            duration = rv.get("duration")
            width = rv.get("width")
            height = rv.get("height")

        # External video hosts — yt-dlp will resolve these at download time.
        # We just flag them as having a known video URL (post.url)
        elif post.url and any(host in post.url for host in (
            "gfycat.com", "redgifs.com", "imgur.com", "streamable.com",
            "youtube.com", "youtu.be", "v.redd.it"
        )):
            video_url = post.url
            # If it's a v.redd.it URL we should have caught it above as is_video,
            # but sometimes Reddit mislabels. Treat as video.
            if "v.redd.it" in post.url:
                is_video = True

        # Imgur .mp4/.gifv direct
        elif post.url and (post.url.endswith(".mp4") or post.url.endswith(".gifv")):
            video_url = post.url.replace(".gifv", ".mp4")
            is_video = True

        else:
            # Not a video post — skip
            return None

        # Skip galleries (multiple images) — not video content
        if is_gallery and not is_video:
            return None

        # Author can be None if deleted
        author = str(post.author) if post.author else "deleted"

        return ClipCandidate(
            post_id=post.id,
            title=post.title.strip(),
            author=author,
            subreddit=str(post.subreddit),
            permalink=f"https://reddit.com{post.permalink}",
            score=post.score,
            created_utc=post.created_utc,
            url=post.url,
            is_video=is_video,
            is_gallery=is_gallery,
            is_nsfw=post.over_18,
            is_quarantined=getattr(post, "quarantine", False),
            video_url=video_url,
            thumbnail_url=post.thumbnail if post.thumbnail not in ("", "self", "default") else None,
            duration_sec=float(duration) if duration else None,
            width=int(width) if width else None,
            height=int(height) if height else None,
            over_18=post.over_18,
        )

    def _passes_filters(self, c: ClipCandidate) -> bool:
        """Apply pre-scraper filters from config."""
        if c.score < self.cfg.min_score:
            log.debug("Skip %s: score %d < %d", c.post_id, c.score, self.cfg.min_score)
            return False
        if c.age_hours > self.cfg.max_age_hours:
            log.debug("Skip %s: age %.1fh > %dh", c.post_id, c.age_hours, self.cfg.max_age_hours)
            return False
        if c.duration_sec and c.duration_sec > self.cfg.max_duration_sec:
            log.debug("Skip %s: duration %.0fs > %ds",
                      c.post_id, c.duration_sec, self.cfg.max_duration_sec)
            return False
        return True

    def rate_limit_info(self) -> dict:
        """Return current rate limit info (requests remaining, reset time)."""
        limits = self.reddit.auth.limits
        return {
            "remaining": limits.get("remaining"),
            "used": limits.get("used"),
            "reset_timestamp": limits.get("reset_timestamp"),
        }
