"""All playback commands and wavelink event listeners.

Everything here is open to everyone in the server — no premium gating.
"""
from __future__ import annotations

import asyncio
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


PAGE_SIZE = 10  # songs per /queue page


def _track_line(position: int, track: wavelink.Playable) -> str:
    length = "LIVE" if track.is_stream else format_ms(track.length)
    return f"`{position}.` {trunc(track.title, 55)} — {length}"


def build_queue_embed(player: MusicPlayer, page: int) -> discord.Embed:
    """One page of /queue, plus a footer showing loop/autoplay/effect state."""
    embed = discord.Embed(title="Queue", color=discord.Color.blurple())

    if player.current is not None:
        cur = player.current
        length = "🔴 LIVE" if cur.is_stream else format_ms(cur.length)
        embed.add_field(name="Now playing", value=f"**{trunc(cur.title)}** — {length}", inline=False)

    tracks = list(player.queue)
    pages = max(1, -(-len(tracks) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = tracks[start : start + PAGE_SIZE]
    if chunk:
        lines = [_track_line(start + i, t) for i, t in enumerate(chunk, start=1)]
        embed.add_field(name="Up next", value="\n".join(lines), inline=False)
    else:
        extra = " Autoplay will pick something similar." if player.radio else ""
        embed.add_field(name="Up next", value="Nothing queued." + extra, inline=False)

    total = sum(t.length for t in tracks if not t.is_stream)
    footer = [f"Page {page + 1}/{pages}", f"{len(tracks)} song(s)", format_ms(total)]
    if player.queue.mode is wavelink.QueueMode.loop:
        footer.append("🔂 Looping song")
    elif player.queue.mode is wavelink.QueueMode.loop_all:
        footer.append("🔁 Looping queue")
    if player.radio:
        footer.append("📻 Autoplay on")
    if player.effect:
        footer.append(f"✨ {player.effect}")
    embed.set_footer(text=" • ".join(footer))
    return embed


class QueuePages(discord.ui.View):
    """◀ ▶ buttons for /queue. Every press re-reads the live queue."""

    def __init__(self, player: MusicPlayer) -> None:
        super().__init__(timeout=180)
        self.player = player
        self.page = 0
        self.message: discord.Message | None = None

    def page_count(self) -> int:
        return max(1, -(-len(self.player.queue) // PAGE_SIZE))

    def render(self) -> discord.Embed:
        self.page = max(0, min(self.page, self.page_count() - 1))
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.page_count() - 1
        return build_queue_embed(self.player, self.page)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        await interaction.response.edit_message(embed=self.render(), view=self)


async def queue_position_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Suggest queued songs by name, so people pick a song instead of counting."""
    guild = interaction.guild
    player = cast("MusicPlayer | None", guild.voice_client) if guild else None
    if player is None or not player.queue:
        return []
    needle = current.strip().lower()
    choices: list[app_commands.Choice[int]] = []
    for position, track in enumerate(player.queue, start=1):
        label = f"{position}. {track.title} — {track.author or 'Unknown'}"
        # Numbers match positions ("2" -> #2, #20, #21...); words match title/artist.
        text = f"{track.title} {track.author or ''}".lower()
        if needle and not (str(position).startswith(needle) or needle in text):
            continue
        choices.append(app_commands.Choice(name=trunc(label, 100), value=position))
        if len(choices) == 25:  # Discord's limit
            break
    return choices


class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        # guild id -> (track identifier, monotonic start time), for spotting
        # SoundCloud Go+ previews that stop after 30 seconds.
        self._started: dict[int, tuple[str, float]] = {}
        # SoundCloud uploads known to be previews; never pick them again.
        self._previews: set[str] = set()
        # YouTube track id -> when we last retried it. yt-dlp failures (like a
        # one-off HTTP 403) often succeed on a second try with a fresh link.
        self._retried: dict[str, float] = {}

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

        # First failure of a YouTube song: try it once more before giving up.
        # yt-dlp fetches a brand-new stream link each attempt, and YouTube's
        # refusals are often one-offs.
        if track.source == "youtube":
            now = time.monotonic()
            if now - self._retried.get(track.identifier, -1e9) > 120:
                self._retried[track.identifier] = now
                log.info("Retrying YouTube track %r once", track.title)
                current = player.current
                if current is None or current.identifier == track.identifier:
                    await player.play(track, add_history=False)
                else:
                    player.queue.put_at(0, track)
                return

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
                    await player.play(fallback, add_history=False)
                    player.queue.loaded = fallback  # /loop repeats the copy that works
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
            await player.play(replacement, start=30_000, add_history=False)  # continue from 0:30
            player.queue.loaded = replacement
        else:
            player.queue.put_at(0, replacement)  # next song already started
        if player.home is not None:
            await safe_send(
                player.home,
                f"✂️ That SoundCloud upload of **{trunc(track.title)}** was only a preview — "
                "switched to another upload.",
            )

    def _looks_like_preview(self, player: MusicPlayer, track: wavelink.Playable) -> bool:
        """Same test the preview swap uses: a long SoundCloud song ending ~30 s in."""
        if player.guild is None or track.source != "soundcloud" or track.length < 60_000:
            return False
        started = self._started.get(player.guild.id)
        if started is None or started[0] != track.identifier:
            return False
        return 20 <= time.monotonic() - started[1] <= 40

    @commands.Cog.listener("on_wavelink_track_end")
    async def _autoplay_on_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        """/autoplay: when the queue runs dry, keep the music going."""
        player = cast("MusicPlayer | None", payload.player)
        if player is None or not player.radio or not player.connected:
            return
        if payload.reason not in ("finished", "stopped"):
            return  # "replaced" = something else already started; "loadFailed" = fallback handles it
        if self._looks_like_preview(player, payload.track):
            return  # the preview swap is about to take over
        await asyncio.sleep(1)  # let wavelink start the next queued song, if there is one
        if player.queue or player.current is not None:
            return
        await self._radio_next(player, payload.track)

    async def _radio_next(self, player: MusicPlayer, last: wavelink.Playable | None) -> None:
        """Play something similar to recent songs, using YouTube's "mix" for a song.

        Seeds from the most recent YouTube song; if the session has only had
        SoundCloud songs, it looks the last one up on YouTube first.
        """
        history = list(player.queue.history or [])
        recent = ([last] if last else []) + history[::-1]
        seed = next((t.identifier for t in recent if t.source == "youtube"), None)

        if seed is None and last is not None:
            query = f"{_clean(last.author or '')} {_clean(last.title)}".strip()
            try:
                found = await wavelink.Playable.search(query, source="ytsearch")
            except wavelink.WavelinkException:
                found = None
            if found and not isinstance(found, wavelink.Playlist):
                seed = found[0].identifier
        if seed is None:
            return

        try:
            mix = await wavelink.Playable.search(f"https://www.youtube.com/watch?v={seed}&list=RD{seed}")
        except wavelink.WavelinkException:
            log.exception("Autoplay: loading the mix for %s failed", seed)
            mix = None
        tracks = list(mix.tracks) if isinstance(mix, wavelink.Playlist) else list(mix or [])

        played = {t.identifier for t in history[-50:]} | {seed}
        if last is not None:
            played.add(last.identifier)
        pick = next((t for t in tracks if t.identifier not in played and not t.is_stream), None)

        if pick is None:
            log.info("Autoplay: nothing new in the mix for %s", seed)
            if player.home is not None:
                await safe_send(player.home, "📻 Autoplay couldn't find a follow-up song. Queue something with `/play`!")
            return
        if player.queue or player.current is not None or not player.connected:
            return  # someone queued something while we were searching
        await player.play(pick)  # goes into history, so the next pick builds on it

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
        await self._enqueue(interaction, query, source, front=False)

    @app_commands.command(
        name="playnext",
        description="Queue a song (or playlist) to play right after the current one.",
    )
    @app_commands.describe(
        query="Search term or link — same as /play",
        source="Where to search when you type words instead of a link",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="YouTube", value="ytsearch"),
            app_commands.Choice(name="SoundCloud", value="scsearch"),
        ]
    )
    @app_commands.guild_only()
    async def playnext(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._enqueue(interaction, query, source, front=True)

    async def _enqueue(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None,
        *,
        front: bool,
    ) -> None:
        """Shared by /play (end of the queue) and /playnext (front of the queue)."""
        assert interaction.guild is not None
        member = cast(discord.Member, interaction.user)

        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then try again.", ephemeral=True
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
            # (partial = queue only; /autoplay is handled by this cog instead).
            player.autoplay = wavelink.AutoPlayMode.partial

        if player.home is None:
            player.home = interaction.channel

        # URLs go to Lavalink as-is; plain text is prefixed with the chosen
        # search source. Spotify links are resolved by LavaSrc on the node
        # (metadata -> a matching stream via the `providers` list).
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
            tracks = list(results.tracks)
            if front:
                for i, t in enumerate(tracks):
                    player.queue.put_at(i, t)
                msg = f"⏭️ **{len(tracks)}** tracks from **{trunc(results.name)}** will play next."
            else:
                added = await player.queue.put_wait(results)
                msg = f"➕ Queued **{added}** tracks from **{trunc(results.name)}**."
        else:
            track = results[0]
            if front:
                player.queue.put_at(0, track)
                msg = f"⏭️ **{trunc(track.title)}** will play next."
            else:
                await player.queue.put_wait(track)
                msg = f"➕ Queued **{trunc(track.title)}** (position {len(player.queue)})."
        await interaction.followup.send(msg)

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

        view = QueuePages(player)
        embed = view.render()
        if view.page_count() == 1:
            await interaction.response.send_message(embed=embed)
            return
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

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

    # ------------------------------------------------------------ queue tools

    @staticmethod
    async def _check_position(
        interaction: discord.Interaction, player: MusicPlayer, *positions: int
    ) -> bool:
        if not player.queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return False
        for position in positions:
            if not 1 <= position <= len(player.queue):
                await interaction.response.send_message(
                    f"There's no song #{position} — the queue has {len(player.queue)}. "
                    "Pick one from the list as you type.",
                    ephemeral=True,
                )
                return False
        return True

    @app_commands.command(name="remove", description="Remove a song from the queue.")
    @app_commands.describe(position="The song to remove — start typing its name or number")
    @app_commands.autocomplete(position=queue_position_autocomplete)
    @app_commands.guild_only()
    async def remove(
        self, interaction: discord.Interaction, position: app_commands.Range[int, 1]
    ) -> None:
        player = await get_controllable_player(interaction)
        if player is None or not await self._check_position(interaction, player, position):
            return
        track = player.queue[position - 1]
        player.queue.delete(position - 1)
        await interaction.response.send_message(f"🗑️ Removed **{trunc(track.title)}** (was #{position}).")

    @app_commands.command(name="move", description="Move a song to a different spot in the queue.")
    @app_commands.describe(
        position="The song to move",
        to="Where it should go — it takes that song's place",
    )
    @app_commands.autocomplete(position=queue_position_autocomplete, to=queue_position_autocomplete)
    @app_commands.guild_only()
    async def move(
        self,
        interaction: discord.Interaction,
        position: app_commands.Range[int, 1],
        to: app_commands.Range[int, 1],
    ) -> None:
        player = await get_controllable_player(interaction)
        if player is None or not await self._check_position(interaction, player, position, to):
            return
        if position == to:
            await interaction.response.send_message("It's already there.", ephemeral=True)
            return
        track = player.queue[position - 1]
        player.queue.delete(position - 1)
        player.queue.put_at(to - 1, track)
        await interaction.response.send_message(f"↕️ Moved **{trunc(track.title)}** to #{to}.")

    @app_commands.command(
        name="skipto", description="Jump to a song in the queue, skipping the ones before it."
    )
    @app_commands.describe(position="The song to jump to")
    @app_commands.autocomplete(position=queue_position_autocomplete)
    @app_commands.guild_only()
    async def skipto(
        self, interaction: discord.Interaction, position: app_commands.Range[int, 1]
    ) -> None:
        player = await get_controllable_player(interaction)
        if player is None or not await self._check_position(interaction, player, position):
            return
        for _ in range(position - 1):
            player.queue.delete(0)
        player.queue.loaded = None  # so /loop doesn't hand back the old song
        track = player.queue.get()  # also marks it as the song /loop repeats
        await player.play(track)
        dropped = f" (dropped {position - 1} song(s) before it)" if position > 1 else ""
        await interaction.response.send_message(f"⏭️ Skipped to **{trunc(track.title)}**{dropped}.")

    @app_commands.command(name="clear", description="Empty the queue. The current song keeps playing.")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        count = len(player.queue)
        if count == 0:
            await interaction.response.send_message("The queue is already empty.", ephemeral=True)
            return
        player.queue.clear()
        await interaction.response.send_message(
            f"🧹 Cleared **{count}** song(s). The current song keeps playing."
        )

    # -------------------------------------------------------- playback extras

    @app_commands.command(name="loop", description="Repeat the current song or the whole queue.")
    @app_commands.describe(mode="What to repeat")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Current song", value="song"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return

        if mode.value == "song":
            if player.current is None:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)
                return
            player.queue.mode = wavelink.QueueMode.loop
            player.queue.loaded = player.current
            msg = (
                f"🔂 Looping **{trunc(player.current.title)}**. "
                "`/skip` moves on (and loops the next song); `/loop Off` to stop."
            )
        elif mode.value == "queue":
            player.queue.mode = wavelink.QueueMode.loop_all
            # Wavelink refills a looping queue from the play history. Restart
            # that history at the current song, so the loop is "this song and
            # everything after it" rather than everything played all session.
            history = player.queue.history
            if history is not None:
                history.clear()
                if player.current is not None:
                    history.put(player.current)
            msg = "🔁 Looping the queue — the current song and everything after it."
        else:
            player.queue.mode = wavelink.QueueMode.normal
            msg = "➡️ Loop off."
        await interaction.response.send_message(msg)

    @app_commands.command(name="back", description="Go back to the previous song.")
    @app_commands.guild_only()
    async def back(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        history = player.queue.history
        current = player.current
        items = list(history) if history is not None else []

        # The history ends with the current song (added when it started);
        # the one before that is where we go back to.
        idx = len(items) - 1
        while idx >= 0 and current is not None and items[idx].identifier == current.identifier:
            idx -= 1
        if idx < 0 or history is None:
            await interaction.response.send_message("There's no previous song yet.", ephemeral=True)
            return

        previous = items[idx]
        for _ in range(len(items) - idx):  # drop it and everything after it;
            history.delete(-1)             # play() re-adds it below
        if current is not None:
            player.queue.put_at(0, current)  # the song we left comes straight back after
        player.queue.loaded = previous
        await player.play(previous)

        after = f" **{trunc(current.title)}** is up next." if current is not None else ""
        await interaction.response.send_message(f"⏮️ Back to **{trunc(previous.title)}**.{after}")

    @app_commands.command(
        name="autoplay",
        description="Toggle autoplay: keep playing similar songs when the queue runs out.",
    )
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        player.radio = not player.radio
        if not player.radio:
            await interaction.response.send_message("📻 Autoplay **off**.")
            return
        await interaction.response.send_message(
            "📻 Autoplay **on** — when the queue runs out, I'll keep playing songs "
            "similar to what's been playing."
        )
        if player.current is None and not player.queue:
            await self._radio_next(player, None)  # nothing playing: start right away

    @app_commands.command(name="filter", description="Add a fun audio effect.")
    @app_commands.describe(effect="Which effect (Off removes it)")
    @app_commands.choices(
        effect=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Nightcore (faster, higher)", value="nightcore"),
            app_commands.Choice(name="Vaporwave (slower, lower)", value="vaporwave"),
            app_commands.Choice(name="8D audio (circles your head — use headphones)", value="8d"),
        ]
    )
    @app_commands.guild_only()
    async def filter_(self, interaction: discord.Interaction, effect: app_commands.Choice[str]) -> None:
        player = await get_controllable_player(interaction)
        if player is None:
            return
        filters: wavelink.Filters = player.filters
        # One effect at a time; /bassboost's equalizer is left alone.
        filters.timescale.reset()
        filters.rotation.reset()
        if effect.value == "nightcore":
            filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
        elif effect.value == "vaporwave":
            filters.timescale.set(speed=0.8, pitch=0.8, rate=1.0)
        elif effect.value == "8d":
            filters.rotation.set(rotation_hz=0.2)
        await player.set_filters(filters)

        names = {"nightcore": "Nightcore", "vaporwave": "Vaporwave", "8d": "8D audio"}
        player.effect = names.get(effect.value)
        if player.effect is None:
            await interaction.response.send_message("✨ Effects off.")
        else:
            await interaction.response.send_message(f"✨ Effect set to **{player.effect}**.")


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Music(bot))
