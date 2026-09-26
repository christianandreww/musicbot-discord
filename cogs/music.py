"""All playback commands and wavelink event listeners.

Everything here is open to everyone in the server — no premium gating.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, cast

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

import config
from cogs.lyrics import _clean
from player import (
    MusicPlayer,
    format_ms,
    get_controllable_player,
    parse_timestamp,
    safe_send,
    trunc,
)

if TYPE_CHECKING:
    from bot import MusicBot

log = logging.getLogger("musicbot.music")

# Per-band equalizer gain by boost level. Lavalink accepts -0.25 .. +1.0 per
# band, but much above ~0.35 on the low bands clips audibly.
BASS_GAINS = {"low": 0.10, "medium": 0.20, "high": 0.30}


class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        # guild id -> (track identifier, monotonic start time), for spotting
        # SoundCloud Go+ previews that stop after 30 seconds.
        self._started: dict[int, tuple[str, float]] = {}
        # SoundCloud uploads known to be previews; never pick them again.
        self._previews: set[str] = set()

    # ------------------------------------------------------------------ events

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("Lavalink node ready (session resumed=%s)", payload.resumed)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = cast("MusicPlayer | None", payload.player)
        if player is not None and player.guild is not None:
            self._started[player.guild.id] = (payload.track.identifier, time.monotonic())
        if player is None or player.home is None:
            return

        track = payload.track
        embed = discord.Embed(
            title="Now playing",
            description=f"[{track.title}]({track.uri})" if track.uri else track.title,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="By", value=track.author or "Unknown")
        embed.add_field(
            name="Length",
            value="🔴 LIVE" if track.is_stream else format_ms(track.length),
        )
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        await safe_send(player.home, embed=embed)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(
        self, payload: wavelink.TrackExceptionEventPayload
    ) -> None:
        """Lavalink failed mid-stream: dead source, decode error, geo-block…"""
        player = cast("MusicPlayer | None", payload.player)
        exc = payload.exception
        message = exc.get("message") if isinstance(exc, dict) else str(exc)
        log.warning("Track exception for %r: %s", payload.track.title, message)

        if player is None:
            return

        track = payload.track
        # YouTube keeps breaking playback for self-hosted bots. Instead of
        # just skipping, look the same song up on SoundCloud and slot it in
        # as the very next track (AutoPlayMode.partial then plays it).
        log.info("Failed track source=%r", track.source)
        if track.source != "soundcloud":
            # YouTube uploaders are often labels ("Emotion Entertainment"), not
            # the artist, and most music titles already read "Artist - Song".
            # So search by title, and only prepend the uploader when the title
            # has no artist in it.
            title = _clean(track.title)
            author = _clean((track.author or "").removesuffix(" - Topic"))
            query = title if " - " in title else f"{author} {title}".strip()
            try:
                results = await wavelink.Playable.search(query, source="scsearch")
            except wavelink.WavelinkException:
                log.exception("SoundCloud fallback search failed for %r", query)
                results = None
            log.info("SoundCloud fallback for %r -> %d result(s)", query,
                     0 if not results else len(results))
            candidates = []
            if results and not isinstance(results, wavelink.Playlist):
                candidates = [t for t in results if t.identifier not in self._previews]
            if candidates:
                fallback = candidates[0]
                # The SoundCloud search takes about a second, and by the time
                # it returns Lavalink has already ended the failed track. If
                # autoplay moved on to the next queued song, slot the fallback
                # in right after it; if nothing is playing (the failed song
                # was the last one), start the fallback straight away.
                current = player.current
                if current is None or current.identifier == track.identifier:
                    await player.play(fallback)
                else:
                    player.queue.put_at(0, fallback)
                if player.home is not None:
                    await safe_send(
                        player.home,
                        f"🔁 YouTube wouldn't stream **{trunc(track.title)}** — "
                        f"playing it from SoundCloud instead.",
                    )
                return

        if player.home is not None:
            first_line = (message or "unknown error").splitlines()[0]
            await safe_send(
                player.home,
                f"⚠️ **{trunc(track.title)}** failed to stream ({trunc(first_line, 120)}). "
                "Skipping to the next track.",
            )
        # AutoPlayMode.partial advances the queue automatically.

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        """Catch SoundCloud Go+ previews and swap in a full upload.

        Paid-only SoundCloud songs give non-subscribers a 30-second clip while
        still reporting the full length, so they "finish" suspiciously early.
        When that happens, find another upload of the same song that isn't a
        preview and carry on from the 30-second mark.
        """
        player = cast("MusicPlayer | None", payload.player)
        track = payload.track
        if player is None or player.guild is None or track.source != "soundcloud":
            return
        if payload.reason != "finished" or track.length < 60_000:
            return
        started = self._started.get(player.guild.id)
        if started is None or started[0] != track.identifier:
            return
        elapsed = time.monotonic() - started[1]
        if not 20 <= elapsed <= 40:
            return  # played normally (or was paused/seeked) - not a preview

        log.info("%r looks like a SoundCloud preview (stopped after %.0fs)", track.title, elapsed)
        self._previews.add(track.identifier)

        query = f"{track.author or ''} {track.title}".strip()
        try:
            results = await wavelink.Playable.search(query, source="scsearch")
        except wavelink.WavelinkException:
            results = None
        candidates = []
        if results and not isinstance(results, wavelink.Playlist):
            candidates = [t for t in results if t.identifier not in self._previews]

        if not candidates:
            if player.home is not None:
                await safe_send(
                    player.home,
                    f"✂️ **{trunc(track.title)}** is only a 30-second preview on SoundCloud "
                    "and I couldn't find a full upload. Skipping.",
                )
            return

        replacement = candidates[0]
        current = player.current
        if current is None or current.identifier == track.identifier:
            await player.play(replacement, start=30_000)  # pick up where the clip ended
        else:
            player.queue.put_at(0, replacement)  # next song already started
        if player.home is not None:
            await safe_send(
                player.home,
                f"✂️ That SoundCloud upload of **{trunc(track.title)}** was only a preview — "
                "switched to another upload.",
            )

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        player = cast("MusicPlayer | None", payload.player)
        log.warning("Track stuck (>%sms): %r", payload.threshold, payload.track.title)
        if player is None:
            return
        if player.home is not None:
            await safe_send(player.home, f"⚠️ **{trunc(payload.track.title)}** froze — skipping.")
        await player.skip(force=True)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        """Fired after `inactive_timeout` seconds with nothing playing."""
        p = cast(MusicPlayer, player)
        if p.stay:
            return
        if p.home is not None:
            await safe_send(
                p.home, "👋 Left the voice channel after a while of silence. (`/stay` keeps me in.)"
            )
        await p.disconnect()

    # ---------------------------------------------------------------- commands

    @app_commands.command(
        name="play",
        description="Play a song, podcast, playlist, or direct audio link.",
    )
    @app_commands.describe(
        query="Search term, or a YouTube / Spotify / Apple Music / Deezer / SoundCloud / .mp3 URL",
        source="Where to search when you type words instead of a link",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="YouTube", value="ytsearch"),
            app_commands.Choice(name="SoundCloud", value="scsearch"),
        ]
    )
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild is not None
        member = cast(discord.Member, interaction.user)

        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then run `/play` again.", ephemeral=True
            )
            return

        channel = member.voice.channel
        player = cast("MusicPlayer | None", interaction.guild.voice_client)

        if player is not None and player.connected and player.channel != channel:
            await interaction.response.send_message(
                f"I'm already playing in {player.channel.mention}. "
                "Join me there, or use `/stop` first.",
                ephemeral=True,
            )
            return

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            await interaction.response.send_message(
                f"I need the **Connect** and **Speak** permissions in {channel.mention}.",
                ephemeral=True,
            )
            return

        # Searching/connecting can exceed the 3-second interaction window.
        await interaction.response.defer()

        if player is None or not player.connected:
            try:
                player = await channel.connect(cls=MusicPlayer, self_deaf=True)
            except wavelink.InvalidNodeException:
                await interaction.followup.send(
                    "🔌 Lavalink is offline — start it and try again."
                )
                return
            except (
                discord.ClientException,
                wavelink.ChannelTimeoutException,
                wavelink.InvalidChannelStateException,
            ):
                await interaction.followup.send(
                    "I couldn't connect to the voice channel. Please try again."
                )
                return
            # Continue through the queue automatically when a track ends
            # (partial = queue only, no algorithmic recommendations).
            player.autoplay = wavelink.AutoPlayMode.partial

        if player.home is None:
            player.home = interaction.channel

        # URLs go to Lavalink as-is; plain text is prefixed with the chosen
        # search source. Spotify/Apple/Deezer links are resolved by LavaSrc on
        # the node (metadata -> a matching stream via the `providers` list).
        try:
            results: wavelink.Search = await wavelink.Playable.search(
                query, source=source.value if source else "ytsearch"
            )
        except wavelink.LavalinkLoadException as exc:
            log.warning("Load failed for %r: %s", query, exc)
            await interaction.followup.send(f"❌ Couldn't load that: `{exc.error}`")
            return
        except wavelink.InvalidNodeException:
            await interaction.followup.send("🔌 Lavalink is offline. Try again shortly.")
            return

        if not results:
            await interaction.followup.send(f"🔍 No results for `{trunc(query, 80)}`.")
            return

        if isinstance(results, wavelink.Playlist):
            added = await player.queue.put_wait(results)
            await interaction.followup.send(
                f"➕ Queued **{added}** tracks from **{trunc(results.name)}**."
            )
        else:
            track = results[0]
            await player.queue.put_wait(track)
            await interaction.followup.send(
                f"➕ Queued **{trunc(track.title)}** (position {len(player.queue)})."
            )

        if not player.playing:
            await player.play(player.queue.get())

    @app_commands.command(name="pause", description="Pause or resume playback.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        if player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        await player.pause(not player.paused)
        await interaction.response.send_message("⏸️ Paused." if player.paused else "▶️ Resumed.")

    @app_commands.command(name="skip", description="Skip the current track.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        if player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        title = player.current.title
        await player.skip(force=True)
        await interaction.response.send_message(f"⏭️ Skipped **{trunc(title)}**.")

    @app_commands.command(
        name="seek",
        description="Jump to a position — handy for long podcasts. e.g. 12:30",
    )
    @app_commands.describe(position="Seconds, or m:ss, or h:mm:ss")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        if player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if player.current.is_stream:
            await interaction.response.send_message(
                "Live streams can't be seeked.", ephemeral=True
            )
            return

        ms = parse_timestamp(position)
        if ms is None:
            await interaction.response.send_message(
                "Use a format like `90`, `12:30`, or `1:05:00`.", ephemeral=True
            )
            return
        if ms > player.current.length:
            await interaction.response.send_message(
                f"That's past the end ({format_ms(player.current.length)}).", ephemeral=True
            )
            return

        await player.seek(ms)
        await interaction.response.send_message(f"⏩ Jumped to **{format_ms(ms)}**.")

    @app_commands.command(name="nowplaying", description="Show the current track and progress.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        player = cast("MusicPlayer | None", interaction.guild.voice_client)
        if player is None or player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        track = player.current
        embed = discord.Embed(
            title=trunc(track.title, 250),
            url=track.uri or None,
            description=track.author or "",
            color=discord.Color.blurple(),
        )
        if track.is_stream:
            embed.add_field(name="Position", value="🔴 LIVE")
        else:
            done = int(player.position / track.length * 20) if track.length else 0
            bar = "▬" * done + "🔘" + "▬" * (20 - done)
            embed.add_field(
                name="Position",
                value=f"`{format_ms(int(player.position))} / {format_ms(track.length)}`\n{bar}",
                inline=False,
            )
        if player.paused:
            embed.set_footer(text="Paused")
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Show what's coming up.")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        player = cast("MusicPlayer | None", interaction.guild.voice_client)

        if player is None or (player.current is None and not player.queue):
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        embed = discord.Embed(title="Queue", color=discord.Color.blurple())

        if player.current is not None:
            length = "🔴 LIVE" if player.current.is_stream else format_ms(player.current.length)
            embed.add_field(
                name="Now playing",
                value=f"**{trunc(player.current.title)}** — {length}",
                inline=False,
            )

        upcoming = list(player.queue)[:10]
        if upcoming:
            lines = [
                f"`{i}.` {trunc(t.title)} — {format_ms(t.length)}"
                for i, t in enumerate(upcoming, start=1)
            ]
            remaining = len(player.queue) - len(upcoming)
            if remaining > 0:
                lines.append(f"…and **{remaining}** more")
            embed.add_field(name="Up next", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="shuffle", description="Shuffle the queue.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        if len(player.queue) < 2:
            await interaction.response.send_message(
                "Not enough tracks queued to shuffle.", ephemeral=True
            )
            return
        player.queue.shuffle()
        await interaction.response.send_message("🔀 Shuffled the queue.")

    @app_commands.command(name="volume", description="Set playback volume (0–150).")
    @app_commands.describe(level="100 = normal")
    @app_commands.guild_only()
    async def volume(
        self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]
    ) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        await player.set_volume(level)
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @app_commands.command(name="bassboost", description="Apply a bass-boost equalizer.")
    @app_commands.describe(level="How hard the low end should hit")
    @app_commands.choices(
        level=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Low", value="low"),
            app_commands.Choice(name="Medium", value="medium"),
            app_commands.Choice(name="High", value="high"),
        ]
    )
    @app_commands.guild_only()
    async def bassboost(
        self, interaction: discord.Interaction, level: app_commands.Choice[str]
    ) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return

        filters: wavelink.Filters = player.filters
        if level.value == "off":
            filters.equalizer.reset()
        else:
            gain = BASS_GAINS[level.value]
            filters.equalizer.set(
                bands=[
                    {"band": 0, "gain": gain},          # 25 Hz
                    {"band": 1, "gain": gain},          # 40 Hz
                    {"band": 2, "gain": gain * 0.75},   # 63 Hz
                    {"band": 3, "gain": gain * 0.50},   # 100 Hz
                    {"band": 4, "gain": gain * 0.25},   # 160 Hz
                ]
            )

        await player.set_filters(filters)
        await interaction.response.send_message(f"🎚️ Bass boost set to **{level.name}**.")

    @app_commands.command(
        name="stay", description="Toggle staying in the channel instead of leaving when idle."
    )
    @app_commands.guild_only()
    async def stay(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return

        player.stay = not player.stay
        # Disabling the timeout means `on_wavelink_inactive_player` never fires.
        player.inactive_timeout = None if player.stay else config.INACTIVE_TIMEOUT

        await interaction.response.send_message(
            "🕐 I'll stay connected until `/stop`."
            if player.stay
            else "🕐 Normal idle timeout restored."
        )

    @app_commands.command(name="stop", description="Stop, clear the queue, and leave.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return

        player.queue.clear()
        player.stay = False
        await player.disconnect()
        await interaction.response.send_message(
            "⏹️ Stopped playback, cleared the queue, and left the channel."
        )


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Music(bot))
