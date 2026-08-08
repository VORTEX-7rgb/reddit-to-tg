"""
Video downloader — wraps yt-dlp to fetch videos from Reddit and other hosts.
yt-dlp handles v.redd.it, gfycat, redgifs, imgur, streamable, youtube — all
in one call. Falls back to direct URL fetch for plain .mp4 links.

Downloads to a temp file, validates, and returns the local Path on success.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .reddit_scraper import ClipCandidate

log = logging.getLogger(__name__)


@dataclass
class DownloadedVideo:
    path: Path
    duration_sec: float
    width: int
    height: int
    size_bytes: int
    source_url: str


class VideoDownloader:
    def __init__(self, work_dir: Path, max_duration_sec: int = 120):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.max_duration_sec = max_duration_sec
        # yt-dlp binary — must be on PATH or installed via pip
        self.yt_dlp_bin = shutil.which("yt-dlp") or "yt-dlp"
        self.ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

    def download(self, candidate: ClipCandidate) -> DownloadedVideo | None:
        """
        Download the video for this candidate. Returns None on failure.
        Tries yt-dlp first (handles v.redd.it, gfycat, etc.),
        falls back to direct .mp4 fetch.
        """
        url = candidate.video_url or candidate.url
        if not url:
            log.warning("Candidate %s has no video URL.", candidate.post_id)
            return None

        # Reddit-hosted videos come split (video + audio). yt-dlp merges via ffmpeg.
        # We force mp4 output for Telegram compatibility.
        out_path = self.work_dir / f"{candidate.post_id}.mp4"

        # Skip if already downloaded (idempotent re-runs)
        if out_path.exists() and out_path.stat().st_size > 50_000:
            log.info("Reusing cached download: %s", out_path)
            return self._probe(out_path, url)

        # Method 1: yt-dlp (most robust)
        ok = self._try_yt_dlp(url, out_path, candidate)
        if not ok:
            # Method 2: direct .mp4 fetch
            ok = self._try_direct(url, out_path)

        if not ok or not out_path.exists():
            log.error("All download methods failed for %s", candidate.post_id)
            return None

        return self._probe(out_path, url)

    def _try_yt_dlp(self, url: str, out_path: Path, candidate: ClipCandidate) -> bool:
        """Run yt-dlp with sensible defaults. Returns True on success."""
        # Template: write to {post_id}.mp4
        out_template = str(out_path.with_suffix(""))  # yt-dlp adds ext

        cmd = [
            self.yt_dlp_bin,
            "--no-warnings",
            "--no-playlist",
            "--no-progress",
            "--newline",
            # Force best mp4 ≤720p, ≤120s (for Telegram retention)
            "-f", "best[ext=mp4][height<=720]/best[height<=720]/best",
            # Re-encode if needed to ensure h264+aac (Telegram-friendly)
            "--merge-output-format", "mp4",
            # Limit to 60 seconds if vertical phone clip (safety filter handles this)
            # but yt-dlp can also slice: --download-sections "*0-120"
            "--download-sections", f"*0-{self.max_duration_sec}",
            "--force-keyframes-at-cuts",
            "-o", out_template,
            url,
        ]
        log.info("yt-dlp downloading %s -> %s", url, out_path)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                check=False,
            )
            if result.returncode != 0:
                log.warning("yt-dlp failed (rc=%d): %s",
                            result.returncode, result.stderr[:500])
                return False
            # yt-dlp may write .mp4 or .webm; check both
            if out_path.exists():
                return True
            for ext in (".mp4", ".webm", ".mkv"):
                p = out_path.with_suffix(ext)
                if p.exists():
                    if ext != ".mp4":
                        # Transcode to mp4 via ffmpeg
                        self._transcode(p, out_path)
                        p.unlink()
                    return True
            return False
        except subprocess.TimeoutExpired:
            log.warning("yt-dlp timed out for %s", url)
            return False
        except FileNotFoundError:
            log.error("yt-dlp binary not found. Install with: pip install yt-dlp")
            return False

    def _try_direct(self, url: str, out_path: Path) -> bool:
        """Direct HTTP fetch for plain .mp4 URLs."""
        if not url.lower().endswith(".mp4"):
            return False
        log.info("Direct-fetching %s", url)
        try:
            r = requests.get(url, stream=True, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            })
            r.raise_for_status()
            with out_path.open("wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            return out_path.exists() and out_path.stat().st_size > 50_000
        except Exception as e:
            log.warning("Direct fetch failed: %s", e)
            return False

    def _transcode(self, src: Path, dst: Path) -> bool:
        """Transcode any video file to h264+aac mp4 via ffmpeg."""
        cmd = [
            self.ffmpeg_bin, "-y", "-i", str(src),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",   # web-optimize for streaming
            str(dst),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
            return True
        except Exception as e:
            log.error("Transcode failed: %s", e)
            return False

    def _probe(self, path: Path, source_url: str) -> DownloadedVideo | None:
        """Run ffprobe to get duration, dimensions, size."""
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        cmd = [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=width,height",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if r.returncode != 0:
                log.warning("ffprobe failed: %s", r.stderr[:200])
                # Still return basic info
                return DownloadedVideo(
                    path=path,
                    duration_sec=0.0,
                    width=0, height=0,
                    size_bytes=path.stat().st_size,
                    source_url=source_url,
                )
            duration = 0.0
            width = 0
            height = 0
            for line in r.stdout.splitlines():
                if line.startswith("duration="):
                    try: duration = float(line.split("=")[1])
                    except ValueError: pass
                elif line.startswith("width="):
                    try: width = int(line.split("=")[1])
                    except ValueError: pass
                elif line.startswith("height="):
                    try: height = int(line.split("=")[1])
                    except ValueError: pass
            return DownloadedVideo(
                path=path,
                duration_sec=duration,
                width=width, height=height,
                size_bytes=path.stat().st_size,
                source_url=source_url,
            )
        except Exception as e:
            log.error("ffprobe exception: %s", e)
            return None

    def cleanup(self, path_or_post_id: Path | str | None) -> None:
        """Delete downloaded video file and any related temp/part files matching post_id."""
        if not path_or_post_id:
            return
        try:
            if isinstance(path_or_post_id, Path):
                post_id = path_or_post_id.stem
            else:
                post_id = str(path_or_post_id)

            # Match any file starting with post_id (e.g. 1abc23.mp4, 1abc23.webm, 1abc23.part)
            for p in self.work_dir.glob(f"{post_id}*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    log.info("Wiped media file: %s", p.name)
        except Exception as e:
            log.warning("Failed to cleanup files for %s: %s", path_or_post_id, e)
