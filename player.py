"""Custom Player subclass + small helpers shared by the cogs."""
from __future__ import annotations

import logging
from typing import cast

import discord
import wavelink

import config

log = logging.getLogger("musicbot.player")


class MusicPlayer(wavelink.Player):
    """A wavelink.Player that remembers where to post "now playing" messages.

    Attributes:
        home: The text channel used for announcements.
        stay: When True the bot never auto-disconnects (see /stay).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.home: discord.abc.Messageable | None = None
        self.stay: bool = False
        # /autoplay: when the queue runs out, keep playing similar songs.
        self.radio: bool = False
        # /filter: name of the active audio effect, shown in /queue.
        self.effect: str | None = None
        # After this many seconds with nothing playing, wavelink fires
        # `on_wavelink_inactive_player`. /stay sets this to None (disabled).
        self.inactive_timeout = config.INACTIVE_TIMEOUT


def format_ms(ms: int) -> str:
    """Milliseconds -> `m:ss` or `h:mm:ss`."""
    seconds = ms // 1000
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def parse_timestamp(value: str) -> int | None:
    """`90`, `1:30`, or `1:02:03` -> milliseconds. None if unparseable."""
    parts = value.strip().split(":")
    if not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total * 1000


def trunc(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def safe_send(channel: discord.abc.Messageable, *args, **kwargs) -> None:
    """Send a message, swallowing HTTP/permission errors so event listeners
    (which have no interaction to reply to) can never crash the bot."""
    try:
        await channel.send(*args, **kwargs)
    except discord.HTTPException as exc:
        log.debug("safe_send failed: %s", exc)


async def get_controllable_player(interaction: discord.Interaction) -> MusicPlayer | None:
    """Return this guild's player if the invoker may control it.

    Otherwise reply with the appropriate ephemeral error and return None.
    """
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True
        )
        return None

    player = cast("MusicPlayer | None", interaction.guild.voice_client)
    if player is None or not player.connected:
        await interaction.response.send_message(
            "I'm not connected to a voice channel. Use `/play` first.", ephemeral=True
        )
        return None

    member = cast(discord.Member, interaction.user)
    if member.voice is None or member.voice.channel != player.channel:
        await interaction.response.send_message(
            f"You need to be in {player.channel.mention} to control playback.",
            ephemeral=True,
        )
        return None

    return player
