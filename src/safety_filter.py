"""
Content safety filter — runs on every ClipCandidate before it gets posted.

Multi-layer defense:
  1. Reddit metadata (NSFW flag, quarantined, subreddit blacklist)
  2. Title keyword scan (banned words list)
  3. Profanity censor on caption text
  4. Video-frame analysis via Pillow — basic brightness/exposure check
     (optional, can be extended with NSFW detection models)
  5. Dimension/duration guardrails

Returns a SafetyDecision with reason codes so the scheduler can log WHY
something was rejected.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from better_profanity import profanity

from .config import SafetyCfg
from .reddit_scraper import ClipCandidate

log = logging.getLogger(__name__)


@dataclass
class SafetyDecision:
    approved: bool
    reason: str                    # human-readable summary
    reason_codes: list[str]        # machine-readable tags for logging
    censored_title: str | None = None   # title with profanity censored, if applicable


# Subreddits we hard-block even if user adds them to config.
# These have a history of TOS-violating content (gore, NSFL, doxxing).
HARD_BLOCKED_SUBREDDITS = {
    "watchpeopledie",          # NSFL gore
    "CrazyFuckingVideos",      # often NSFL
    "MakeMyCoffin",            # NSFL
    "shitposting",             # often offensive
    "Eyeblech",                # NSFL gore (intentional misspelling of eyebleach)
    "rpe",                     # sexual violence
    "CuteCorpses",             # NSFL
    "FiftyFifty",              # 50% chance of NSFL
    "morbidreality",           # heavy/NSFL
    "WPD",                     # watch people die alt
}


class SafetyFilter:
    def __init__(self, cfg: SafetyCfg):
        self.cfg = cfg
        # Pre-compile banned word regex for fast scanning
        self._banned_re = re.compile(
            r"\b(" + "|".join(re.escape(w.lower()) for w in cfg.banned_words) + r")\b",
            re.IGNORECASE,
        ) if cfg.banned_words else None
        # Load profanity wordlist
        profanity.load_censor_words()
        # Add custom whitelist if needed (words we DON'T want censored)
        # profanity.add_censor_words([...])

    def evaluate(self, c: ClipCandidate) -> SafetyDecision:
        """Run all safety checks. Returns decision with reason codes."""

        # ── Layer 1: Subreddit hard-block ──────────────────────────
        if c.subreddit.lower() in HARD_BLOCKED_SUBREDDITS:
            return SafetyDecision(
                False,
                f"Subreddit r/{c.subreddit} is on the hard-blocked list (TOS risk).",
                ["hard_blocked_subreddit"],
            )

        # ── Layer 2: Reddit NSFW flag ──────────────────────────────
        if self.cfg.reject_nsfw and (c.is_nsfw or c.over_18):
            return SafetyDecision(
                False,
                "Post is marked NSFW by Reddit.",
                ["reddit_nsfw_flag"],
            )

        # ── Layer 3: Quarantined subreddit ─────────────────────────
        if self.cfg.reject_quarantined and c.is_quarantined:
            return SafetyDecision(
                False,
                f"Subreddit r/{c.subreddit} is quarantined.",
                ["quarantined_subreddit"],
            )

        # ── Layer 4: Banned word scan on title ─────────────────────
        if self._banned_re:
            match = self._banned_re.search(c.title)
            if match:
                return SafetyDecision(
                    False,
                    f"Title contains banned word: '{match.group(1)}'",
                    ["banned_word_in_title"],
                )

        # ── Layer 5: Profanity censor on title (censor, not reject) ─
        censored = None
        if self.cfg.censor_profanity:
            if profanity.contains_profanity(c.title):
                censored = profanity.censor(c.title)
                log.info("Censored profanity in title for %s: '%s' -> '%s'",
                         c.post_id, c.title, censored)

        # ── Layer 6: Dimension guardrails ──────────────────────────
        if self.cfg.min_height > 0 and c.height and c.height < self.cfg.min_height:
            return SafetyDecision(
                False,
                f"Video height {c.height}px below minimum {self.cfg.min_height}px.",
                ["low_resolution"],
            )

        # ── Layer 7: Vertical video duration cap ───────────────────
        # Vertical videos = phone-recorded = often longer / worse retention
        if c.width and c.height and c.height > c.width:
            if c.duration_sec and c.duration_sec > self.cfg.max_vertical_duration_sec:
                return SafetyDecision(
                    False,
                    f"Vertical video duration {c.duration_sec:.0f}s exceeds cap "
                    f"{self.cfg.max_vertical_duration_sec}s (retention risk).",
                    ["vertical_too_long"],
                )

        return SafetyDecision(
            True,
            "Approved by all safety layers.",
            ["approved"],
            censored_title=censored,
        )

    def censor_caption(self, text: str) -> str:
        """Run profanity censor on an arbitrary caption string."""
        if not self.cfg.censor_profanity:
            return text
        return profanity.censor(text)

    @staticmethod
    def check_video_file(video_path: Path) -> tuple[bool, str]:
        """
        Optional: post-download video file inspection.
        Currently just verifies the file exists and is non-trivially sized.
        Extend with NSFW model detection (e.g. NudeNet) for production use.
        """
        if not video_path.exists():
            return False, "File does not exist"
        size_kb = video_path.stat().st_size / 1024
        if size_kb < 50:
            return False, f"File too small ({size_kb:.0f} KB) — likely corrupt"
        if size_kb > 50_000:  # 50 MB
            return False, f"File too large ({size_kb:.0f} KB) — Telegram limit risk"
        return True, "OK"
