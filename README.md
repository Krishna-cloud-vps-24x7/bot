# Discord VPS Manager Bot

A production-oriented Discord bot (Python) to create and manage container-based VPS instances (Ubuntu & Debian) on a host using Docker. If the host lacks a public IPv4 or published SSH port, the bot provides tmate-based SSH/web access as a fallback.

> ⚠️ **Security notice**: This project interacts with the Docker socket and can control the host. Use caution. See "Security" section.

## Files in this repo
- `bot.py` — Main bot.
- `requirements.txt` — Python deps.
- `.env.template` — Template; rename to `.env`.
- `Dockerfile` — Build image for bot.
- `docker-compose.yml` — optional compose to run container.
- `.gitignore`, `README.md`.

## Quick start (recommended: Docker)

1. Copy `.env.template` -> `.env` and fill `DISCORD_TOKEN` and optional values.
2. Build image:
   ```bash
   docker build -t discord-vps-bot:latest .
