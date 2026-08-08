# Karmabot — Reddit → Telegram Video Pipeline

Automated pipeline that scrapes top Reddit video posts, runs a multi-layer
safety filter, downloads via yt-dlp, and posts 1 video every 2 hours
(max 12/day) to your Telegram channel — with full Reddit credit and
anti-ban jitter baked in.

## Quick Start

```bash
# 1. On your VPS (Ubuntu 22.04+):
git clone <this-repo> /tmp/karmabot
cd /tmp/karmabot
sudo bash installer.sh

# 2. Edit config with your Reddit + Telegram credentials
sudo nano /opt/karmabot/config.yaml

# 3. First-run Telegram login (interactive — needs phone code)
sudo -u karmabot /opt/karmabot/venv/bin/python -m src.scheduler \
  --config /opt/karmabot/config.yaml

# 4. Start the timer
sudo systemctl enable --now karmabot.timer

# 5. Watch logs
sudo journalctl -u karmabot.service -f
```

## Architecture

```
            ┌─────────────┐
            │  systemd    │  fires every 2h ± 15min jitter
            │  timer      │  (anti-ban humanization)
            └──────┬──────┘
                   │
                   ▼
            ┌─────────────┐
            │ scheduler   │  single cycle: pick best → post → exit
            │ .py         │  (crash-safe: schedule survives process death)
            └──────┬──────┘
                   │
       ┌───────────┼───────────┬───────────┐
       ▼           ▼           ▼           ▼
   ┌───────┐  ┌────────┐  ┌────────┐  ┌─────────┐
   │ PRAW  │  │ safety │  │ yt-dlp │  │Telethon │
   │scraper│  │ filter │  │ download│  │ upload  │
   └───┬───┘  └────┬───┘  └────┬───┘  └────┬────┘
       │           │           │            │
       ▼           ▼           ▼            ▼
   Reddit API   7-layer    ffmpeg       Telegram
   (read-only)  filter    transcode      user API
```

## Anti-Ban Strategy (baked in)

| Layer | Mechanism |
|-------|-----------|
| Cadence | Hard cap 12 posts/24h, 1 every 2h |
| Jitter | ±15min random delay before each post |
| Quiet hours | Configurable UTC hours to skip |
| Dedup | `state.json` tracks every posted Reddit ID |
| Reddit API | Read-only PRAW, respects 100 req/min limit |
| Telegram API | User-account (Telethon), not bot — higher limits, looks organic |
| Content safety | 7-layer filter (see safety_filter.py) |
| Credit | Every post includes u/author + r/sub + permalink (fair-use defense) |

## Safety Filter Layers

1. **Subreddit hard-block** — TOS-risk subs (watchpeopledie, etc.) auto-rejected
2. **Reddit NSFW flag** — auto-reject if marked NSFW
3. **Quarantined subs** — auto-reject
4. **Banned word scan** — title regex against configurable list (gore, beheading, etc.)
5. **Profanity censor** — `better_profanity` censors slurs in caption
6. **Resolution floor** — configurable minimum height (defaults to 0 / no minimum height restriction)
7. **Vertical duration cap** — vertical videos capped at 60s (retention dropoff)

## Files

```
.
├── config.example.yaml       # Template config — copy to config.yaml
├── installer.sh              # One-shot setup script (run as root)
├── requirements.txt
├── src/
│   ├── config.py             # Typed config loader + validation
│   ├── reddit_scraper.py     # PRAW wrapper, returns ClipCandidate list
│   ├── safety_filter.py      # 7-layer safety gate
│   ├── video_downloader.py   # yt-dlp + ffmpeg, fallback to direct .mp4
│   ├── tg_uploader.py        # Telethon async uploader
│   ├── scheduler.py          # Main orchestrator (one cycle per invocation)
│   └── utils.py              # State, logging, caption builder, disk cleanup
└── systemd/
    ├── karmabot.service      # oneshot service unit
    └── karmabot.timer        # every-2h timer with jitter
```

## Configuration

See `config.example.yaml` for all options. Critical fields:

| Key | Where to get it |
|-----|-----------------|
| `reddit.client_id` | https://www.reddit.com/prefs/apps → create "script" app |
| `reddit.client_secret` | Same as above |
| `telegram.api_id` | https://my.telegram.org/apps |
| `telegram.api_hash` | Same as above |
| `telegram.channel_username` | Your public channel (you must be admin) |

## Operations

```bash
# Check timer status
sudo systemctl list-timers karmabot.timer

# Manually trigger one cycle (for testing)
sudo systemctl start karmabot.service

# View recent logs
sudo journalctl -u karmabot.service --since "1 hour ago"

# Stop the timer
sudo systemctl stop karmabot.timer

# Check state (posted history)
sudo cat /var/lib/karmabot/state.json | jq '.history[-10:]'

# Disk usage
sudo du -sh /var/lib/karmabot/media
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `FloodWaitError: must wait N seconds` | Telegram rate-limited you. Bot will auto-pause. Don't post manually during this window. |
| `ChannelPrivateError` | Your user account isn't admin of the channel. Add it as admin with "Post Messages" permission. |
| yt-dlp fails on v.redd.it | Update yt-dlp: `pip install -U yt-dlp`. Reddit changes their video CDN often. |
| No candidates found | Lower `min_score` in config, or wait — Reddit's `top/day` refreshes hourly. |
| Telegram session expired | Delete `karmabot_session.session` and re-run interactive login. |

## Legal & Compliance Notes

- **Copyright**: Reddit-hosted videos are user-generated content under Reddit's TOS. Posting to a public Telegram channel with full credit (u/author + r/sub + link) is generally fair use but not legally bulletproof. If a copyright holder reports, Telegram may remove the post.
- **NSFW/Gore**: This pipeline is configured to auto-reject NSFW and a wide list of gore-related keywords. You are responsible for reviewing the `banned_words` list and adding anything specific to your jurisdiction.
- **Telegram TOS**: Don't post more than the configured 12/day cap. Don't post sexual content, even with safety filters. Don't post content that promotes violence. Telegram can permanently delete your channel AND your personal account for repeated violations.
- **Reddit TOS**: PRAW with read-only access is fully compliant. Don't add scraping that bypasses rate limits.

## License

MIT — see source files. Use at your own risk.
