"""
Central config loader. Reads config.yaml (or env-overridden path),
validates required fields, and exposes a typed Config object.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass
class RedditCfg:
    client_id: str
    client_secret: str
    user_agent: str
    subreddits: list[str]
    min_score: int
    max_age_hours: int
    video_only: bool
    max_duration_sec: int


@dataclass
class TelegramCfg:
    api_id: int
    api_hash: str
    phone_number: str
    channel_username: str | None
    channel_id: int | None
    session_name: str


@dataclass
class ScheduleCfg:
    interval_hours: int
    max_daily_posts: int
    quiet_hours_utc: list[int]
    jitter_minutes: int


@dataclass
class SafetyCfg:
    banned_words: list[str]
    reject_nsfw: bool
    reject_quarantined: bool
    censor_profanity: bool
    min_height: int
    max_vertical_duration_sec: int


@dataclass
class CaptionCfg:
    template: str
    hashtag_pool: list[str]
    hashtags_per_post: int
    channel_footer: str
    max_length: int


@dataclass
class StorageCfg:
    data_dir: str
    retain_days: int
    max_disk_mb: int


@dataclass
class LoggingCfg:
    level: str
    file: str
    max_size_mb: int
    keep_backups: int


@dataclass
class Config:
    reddit: RedditCfg
    telegram: TelegramCfg
    schedule: ScheduleCfg
    safety: SafetyCfg
    caption: CaptionCfg
    storage: StorageCfg
    logging: LoggingCfg
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def state_file(self) -> Path:
        return Path(self.storage.data_dir) / "state.json"

    @property
    def media_dir(self) -> Path:
        return Path(self.storage.data_dir) / "media"


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"Missing required field '{key}' in {ctx}")
    return d[key]


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("KARMABOT_CONFIG", "config.yaml")
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    with p.open() as f:
        raw = yaml.safe_load(f)

    r = _require(raw, "reddit", "config")
    reddit = RedditCfg(
        client_id=r.get("client_id", "") or "",
        client_secret=r.get("client_secret", "") or "",
        user_agent=r.get("user_agent", "telegram-pipeline/1.0"),
        subreddits=_require(r, "subreddits", "reddit"),
        min_score=r.get("min_score", 500),
        max_age_hours=r.get("max_age_hours", 48),
        video_only=r.get("video_only", True),
        max_duration_sec=r.get("max_duration_sec", 120),
    )

    t = _require(raw, "telegram", "config")
    telegram = TelegramCfg(
        api_id=int(_require(t, "api_id", "telegram")),
        api_hash=_require(t, "api_hash", "telegram"),
        phone_number=_require(t, "phone_number", "telegram"),
        channel_username=t.get("channel_username"),
        channel_id=t.get("channel_id"),
        session_name=t.get("session_name", "karmabot_session"),
    )
    if not telegram.channel_username and not telegram.channel_id:
        raise ConfigError("telegram: provide either channel_username or channel_id")

    s = raw.get("schedule", {})
    schedule = ScheduleCfg(
        interval_hours=s.get("interval_hours", 2),
        max_daily_posts=s.get("max_daily_posts", 12),
        quiet_hours_utc=s.get("quiet_hours_utc", []),
        jitter_minutes=s.get("jitter_minutes", 15),
    )

    sf = raw.get("safety", {})
    safety = SafetyCfg(
        banned_words=sf.get("banned_words", []),
        reject_nsfw=sf.get("reject_nsfw", True),
        reject_quarantined=sf.get("reject_quarantined", True),
        censor_profanity=sf.get("censor_profanity", True),
        min_height=sf.get("min_height", 0),  # 0 = disabled (allow all video resolutions)
        max_vertical_duration_sec=sf.get("max_vertical_duration_sec", 60),
    )

    cap = raw.get("caption", {})
    caption = CaptionCfg(
        template=cap.get("template", "{title}\n\nvia u/{author} on r/{subreddit}\n{url}\n\n{hashtags}"),
        hashtag_pool=cap.get("hashtag_pool", []),
        hashtags_per_post=cap.get("hashtags_per_post", 4),
        channel_footer=cap.get("channel_footer", ""),
        max_length=cap.get("max_length", 800),
    )

    st = raw.get("storage", {})
    storage = StorageCfg(
        data_dir=st.get("data_dir", "/var/lib/karmabot"),
        retain_days=st.get("retain_days", 7),
        max_disk_mb=st.get("max_disk_mb", 2000),
    )

    lg = raw.get("logging", {})
    logging_cfg = LoggingCfg(
        level=lg.get("level", "INFO"),
        file=lg.get("file", "/var/log/karmabot/bot.log"),
        max_size_mb=lg.get("max_size_mb", 10),
        keep_backups=lg.get("keep_backups", 5),
    )

    return Config(
        reddit=reddit,
        telegram=telegram,
        schedule=schedule,
        safety=safety,
        caption=caption,
        storage=storage,
        logging=logging_cfg,
        raw=raw,
    )
