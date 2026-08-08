"""
Telegram uploader via Telethon (user-account API, not bot API).

Why Telethon (user account) instead of python-telegram-bot (bot API)?
  - Bot API can only send files ≤50MB
  - User API can send up to 2GB
  - User API has higher rate limits for posting to channels
  - First post to a public channel "from a user" looks more organic
    than "from a bot"

The session file (karmabot_session.session) is created on first run
via interactive phone-code login. After that it's automatic.

Anti-ban measures baked in:
  - Random delay before each post (jitter)
  - Hard cap on posts per 24h (enforced by scheduler)
  - Quiet-hours respected
  - No reposts (state file tracks posted IDs)
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import InputPeerChannel, PeerChannel
from telethon.errors import (
    FloodWaitError, SlowModeWaitError, ChannelPrivateError,
    UserBannedInChannelError, ChatWriteForbiddenError,
)

from .config import TelegramCfg, ScheduleCfg
from .video_downloader import DownloadedVideo

log = logging.getLogger(__name__)


@dataclass
class PostResult:
    success: bool
    message_id: int | None = None
    error: str | None = None
    flood_wait_seconds: int | None = None


class TelegramUploader:
    def __init__(self, cfg: TelegramCfg, schedule: ScheduleCfg):
        self.cfg = cfg
        self.schedule = schedule
        self.session_path = Path(cfg.session_name)
        self._client: TelegramClient | None = None
        self._entity = None  # resolved channel entity (cached)

    async def _get_client(self) -> TelegramClient:
        if self._client and self._client.is_connected():
            return self._client
        client = TelegramClient(
            str(self.session_path),  # path without .session extension
            self.cfg.api_id,
            self.cfg.api_hash,
        )
        await client.start(phone=self.cfg.phone_number)
        self._client = client
        me = await client.get_me()
        log.info("Telegram logged in as @%s (id=%s)", me.username, me.id)
        return client

    async def _resolve_channel(self):
        """Resolve the target channel entity (cached after first call)."""
        if self._entity:
            return self._entity
        client = await self._get_client()
        if self.cfg.channel_username:
            # Public channel by username
            self._entity = await client.get_entity(self.cfg.channel_username)
        elif self.cfg.channel_id:
            self._entity = await client.get_entity(PeerChannel(self.cfg.channel_id))
        else:
            raise RuntimeError("No channel_username or channel_id configured")
        log.info("Resolved channel: %s (id=%s)",
                 getattr(self._entity, "title", "?"),
                 getattr(self._entity, "id", "?"))
        return self._entity

    async def send_video(
        self,
        video: DownloadedVideo,
        caption: str,
    ) -> PostResult:
        """Send a video to the channel with the given caption."""
        # Pre-post jitter: random delay to look human
        if self.schedule.jitter_minutes > 0:
            wait_sec = random.randint(0, self.schedule.jitter_minutes * 60)
            log.info("Pre-post jitter: sleeping %ds", wait_sec)
            await asyncio.sleep(wait_sec)

        try:
            client = await self._get_client()
            channel = await self._resolve_channel()

            log.info("Sending video %s (%.1fs, %.1fMB) to channel...",
                     video.path.name, video.duration_sec,
                     video.size_bytes / (1024*1024))

            msg = await client.send_file(
                channel,
                file=str(video.path),
                caption=caption[:self.cfg_caption_limit()],
                parse_mode="html",
                # Telegram supports streaming if video is mp4 with faststart
                supports_streaming=True,
                # Don't notify Preview/embed — let Telegram optimize for feed
                # If your channel has slow-mode, this will auto-wait
            )
            log.info("Posted message id=%s", msg.id)
            return PostResult(success=True, message_id=msg.id)

        except FloodWaitError as e:
            log.error("FloodWait: must wait %ds before next post.", e.seconds)
            return PostResult(
                success=False, error="flood_wait",
                flood_wait_seconds=e.seconds,
            )
        except SlowModeWaitError as e:
            log.error("SlowMode active: must wait %ds.", e.seconds)
            return PostResult(
                success=False, error="slow_mode",
                flood_wait_seconds=e.seconds,
            )
        except (ChannelPrivateError, UserBannedInChannelError, ChatWriteForbiddenError) as e:
            log.error("Channel access denied: %s", e)
            return PostResult(success=False, error=f"access_denied: {e}")
        except Exception as e:
            log.exception("Unexpected error sending to Telegram:")
            return PostResult(success=False, error=f"unexpected: {e}")

    def cfg_caption_limit(self) -> int:
        """Telegram caption hard limit is 1024 chars."""
        return 1024

    async def close(self):
        if self._client:
            await self._client.disconnect()
            self._client = None


def send_video_sync(uploader: TelegramUploader, video: DownloadedVideo, caption: str) -> PostResult:
    """Synchronous wrapper for use in non-async scheduler."""
    return asyncio.run(uploader.send_video(video, caption))
