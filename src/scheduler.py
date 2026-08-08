"""
Main orchestrator. One run = pick best candidate, download, safety-check,
upload to Telegram, record state. Designed to be invoked once per posting
interval by a systemd timer (so crashes don't kill the schedule).

Usage:
    python -m src.scheduler --config config.yaml

Exit codes:
    0 = posted successfully (or no candidate available, skipped cleanly)
    1 = config error
    2 = safety rejection (no candidate passed filters)
    3 = download failure
    4 = telegram error (flood wait, channel issue)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from .config import load_config, Config
from .reddit_scraper import RedditScraper, ClipCandidate
from .safety_filter import SafetyFilter, SafetyDecision
from .video_downloader import VideoDownloader, DownloadedVideo
from .tg_uploader import TelegramUploader, PostResult
from .utils import (
    setup_logging, load_state, save_state, record_post,
    posts_today, is_quiet_hour, build_caption, cleanup_old_media,
)

log = logging.getLogger("karmabot.scheduler")


def pick_best_candidate(
    scraper: RedditScraper,
    safety: SafetyFilter,
    already_posted: set[str],
    max_to_inspect: int = 50,
) -> tuple[ClipCandidate | None, SafetyDecision | None, dict]:
    """
    Iterate candidates from scraper, run safety filter on each,
    return the FIRST approved one (sorted by score desc implicitly
    because Reddit .top() is already sorted).
    """
    stats = {"inspected": 0, "rejected_safety": 0, "skipped_posted": 0}
    for candidate in scraper.iter_candidates(already_posted):
        stats["inspected"] += 1
        if stats["inspected"] > max_to_inspect:
            log.info("Inspected %d candidates, none approved — stopping.", max_to_inspect)
            break

        decision = safety.evaluate(candidate)
        if not decision.approved:
            log.info("REJECT %s from r/%s: %s",
                     candidate.post_id, candidate.subreddit, decision.reason)
            stats["rejected_safety"] += 1
            continue

        return candidate, decision, stats

    return None, None, stats


async def run_one_cycle(cfg: Config) -> int:
    """Execute one full scrape->download->safety->post cycle."""
    started_at = datetime.now(timezone.utc)
    log.info("=== Cycle started at %s ===", started_at.isoformat())

    # ── Pre-flight: quiet hours & daily cap ─────────────────────
    state = load_state(cfg.state_file)
    already_posted = set(state.get("posted", []))
    today_count = posts_today(state)
    if today_count >= cfg.schedule.max_daily_posts:
        log.info("Daily cap reached (%d/%d). Skipping cycle.",
                 today_count, cfg.schedule.max_daily_posts)
        return 0
    if is_quiet_hour(cfg):
        log.info("Currently in quiet hours. Skipping cycle.")
        return 0

    # ── Cleanup old media (cheap, runs every cycle) ─────────────
    deleted = cleanup_old_media(cfg.media_dir, cfg.storage.retain_days, cfg.storage.max_disk_mb)
    if deleted:
        log.info("Cleaned up %d old media files.", deleted)

    # ── Scrape + Safety ─────────────────────────────────────────
    scraper = RedditScraper(cfg.reddit)
    safety = SafetyFilter(cfg.safety)
    candidate, decision, stats = pick_best_candidate(scraper, safety, already_posted)

    if candidate is None:
        log.info("No suitable candidate found. Stats: %s", stats)
        return 2

    log.info("SELECTED: %s (r/%s, score=%d, %.1fh old)",
             candidate.title[:60], candidate.subreddit,
             candidate.score, candidate.age_hours)

    # ── Download & Upload with Guaranteed Cleanup ──────────────
    downloader = VideoDownloader(cfg.media_dir, cfg.reddit.max_duration_sec)
    try:
        video = downloader.download(candidate)
        if video is None:
            log.error("Download failed for %s", candidate.post_id)
            return 3

        # Post-download safety check (file integrity)
        file_ok, file_reason = SafetyFilter.check_video_file(video.path)
        if not file_ok:
            log.error("Downloaded file failed check: %s", file_reason)
            return 3

        # ── Build caption ───────────────────────────────────────────
        caption = build_caption(cfg.caption, candidate, decision)
        log.info("Caption (%d chars):\n%s", len(caption), caption)

        # ── Upload to Telegram ──────────────────────────────────────
        uploader = TelegramUploader(cfg.telegram, cfg.schedule)
        try:
            result: PostResult = await uploader.send_video(video, caption)
        finally:
            await uploader.close()

        if not result.success:
            log.error("Telegram post failed: %s (flood_wait=%s)",
                      result.error, result.flood_wait_seconds)
            return 4

        # ── Record success ──────────────────────────────────────────
        state = record_post(state, candidate, result.message_id, decision)
        save_state(cfg.state_file, state)
        log.info("Posted to Telegram (msg_id=%s). Recorded in state.", result.message_id)

        duration_sec = (datetime.now(timezone.utc) - started_at).total_seconds()
        log.info("=== Cycle completed in %.1fs ===", duration_sec)
        return 0

    finally:
        # Immediately wipe out any downloaded video / temp / part files for this post
        downloader.cleanup(candidate.post_id)


def main():
    parser = argparse.ArgumentParser(description="Karmabot single-cycle runner")
    parser.add_argument("--config", "-c", default="config.yaml",
                        help="Path to config.yaml")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    setup_logging(cfg)
    log.info("Karmabot starting. Config: %s", args.config)

    try:
        rc = asyncio.run(run_one_cycle(cfg))
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        rc = 0
    except Exception as e:
        log.exception("Unhandled exception in cycle:")
        rc = 99

    sys.exit(rc)


if __name__ == "__main__":
    main()
