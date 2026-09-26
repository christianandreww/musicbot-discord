"""Configuration, loaded from the environment / .env file."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or ""
if not DISCORD_TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in your .env file (copy .env.example).")

LAVALINK_URI = os.getenv("LAVALINK_URI", "http://127.0.0.1:2333")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

# Your server's id: slash commands appear instantly there instead of taking
# up to an hour to propagate globally. Strongly recommended for private bots.
GUILD_ID = int(os.getenv("GUILD_ID") or 0) or None

# Seconds of silence before the bot leaves the voice channel.
# /stay disables this at runtime; set to 0 to disable it permanently.
INACTIVE_TIMEOUT = int(os.getenv("INACTIVE_TIMEOUT", "300")) or None
