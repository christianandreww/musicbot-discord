"""/lyrics — fetch lyrics for the current track (or any query).

Provider: LRCLIB (https://lrclib.net) — free, no API key, no rate-limit
hassle, and purpose-built for this. Alternatives if you ever need them:
Musixmatch (licensed, paid) or Genius (its API returns metadata only, and
scraping the lyrics pages breaks their terms).

Results are cached in memory for the process lifetime — no Redis needed at
this scale. Misses are cached too, so a track with no lyrics doesn't re-query
every time someone runs the command.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, cast

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from player import MusicPlayer, trunc

if TYPE_CHECKING:
    from bot import MusicBot

log = logging.getLogger("musicbot.lyrics")

LRCLIB = "https://lrclib.net/api"
USER_AGENT = "PersonalMusicBot/1.0 (self-hosted)"
CACHE_TTL = 24 * 3600
CACHE_MAX = 200
PAGE_CHARS = 1800  # embed descriptions cap at 4096; keep pages comfortable

_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*(official|video|audio|lyric|lyrics|visuali[sz]er|live|"
    r"hd|hq|4k|remaster|explicit|clean)[^)\]]*[\)\]]",
    re.IGNORECASE,
)
_FEAT = re.compile(r"\s*(feat\.|ft\.|featuring)\s+.*$", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _NOISE.sub("", text)
    text = _FEAT.sub("", text)
    return text.strip(" -–—|").strip()


def _paginate(lyrics: str) -> list[str]:
    """Split on stanza boundaries into <= PAGE_CHARS chunks."""
    pages: list[str] = []
    current = ""
    for block in lyrics.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= PAGE_CHARS:
            current = candidate
        else:
            if current:
                pages.append(current)
            while len(block) > PAGE_CHARS:  # a single huge stanza
                pages.append(block[:PAGE_CHARS])
                block = block[PAGE_CHARS:]
            current = block
    if current:
        pages.append(current)
    return pages or ["(no lyrics text)"]


class LyricsPaginator(discord.ui.View):
    def __init__(self, embeds: list[discord.Embed], author_id: int) -> None:
        super().__init__(timeout=180)
        self.embeds = embeds
        self.author_id = author_id
        self.index = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.embeds) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Run `/lyrics` yourself to page through them.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = min(len(self.embeds) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)


class Lyrics(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self.http: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, str | None]] = {}

    async def cog_load(self) -> None:
        self.http = httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    async def cog_unload(self) -> None:
        if self.http:
            await self.http.aclose()

    # -------------------------------------------------------------- fetching

    async def _fetch(self, artist: str, title: str, duration_s: int | None) -> str | None:
        assert self.http is not None

        # 1) Exact lookup (duration disambiguates covers and live versions).
        params: dict[str, str | int] = {"artist_name": artist, "track_name": title}
        if duration_s:
            params["duration"] = duration_s
        resp = await self.http.get(f"{LRCLIB}/get", params=params)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("instrumental"):
                return "*(instrumental — no lyrics)*"
            if body.get("plainLyrics"):
                return body["plainLyrics"]

        # 2) Fuzzy fallback.
        resp = await self.http.get(f"{LRCLIB}/search", params={"q": f"{artist} {title}"})
        if resp.status_code == 200:
            for hit in resp.json():
                if hit.get("plainLyrics"):
                    return hit["plainLyrics"]
                if hit.get("instrumental"):
                    return "*(instrumental — no lyrics)*"
        return None

    async def _cached_fetch(self, artist: str, title: str, duration_s: int | None) -> str | None:
        key = f"{artist.lower().strip()}|{title.lower().strip()}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]

        result = await self._fetch(artist, title, duration_s)

        if len(self._cache) >= CACHE_MAX:  # crude eviction: drop the oldest
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[key] = (time.time(), result)
        return result

    # --------------------------------------------------------------- command

    @app_commands.command(
        name="lyrics", description="Show lyrics for the current track (or search)."
    )
    @app_commands.describe(query='Optional: "Artist - Title" (defaults to what\'s playing)')
    @app_commands.guild_only()
    async def lyrics(self, interaction: discord.Interaction, query: str | None = None) -> None:
        assert interaction.guild is not None

        if query:
            artist, sep, title = query.partition("-")
            if not sep:
                artist, title = "", query
            artist, title = _clean(artist), _clean(title)
            duration_s: int | None = None
        else:
            player = cast("MusicPlayer | None", interaction.guild.voice_client)
            if player is None or player.current is None:
                await interaction.response.send_message(
                    'Nothing is playing — try `/lyrics query:"Artist - Title"`.',
                    ephemeral=True,
                )
                return
            track = player.current
            title = _clean(track.title)
            artist = _clean((track.author or "").removesuffix(" - Topic"))
            duration_s = track.length // 1000 if not track.is_stream else None

        await interaction.response.defer()

        try:
            lyrics_text = await self._cached_fetch(artist, title, duration_s)
        except httpx.HTTPError as exc:
            log.warning("Lyrics lookup failed: %s", exc)
            await interaction.followup.send("The lyrics service didn't respond — try again.")
            return

        if not lyrics_text:
            await interaction.followup.send(
                f"🔍 No lyrics found for **{trunc(f'{artist} — {title}'.strip(' —'), 80)}**."
            )
            return

        pages = _paginate(lyrics_text)
        embeds = [
            discord.Embed(
                title=trunc(f"{title} — {artist}".strip(" —"), 250),
                description=page,
                color=discord.Color.blurple(),
            ).set_footer(text=f"Page {i}/{len(pages)} • Lyrics via LRCLIB")
            for i, page in enumerate(pages, start=1)
        ]

        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0])
        else:
            await interaction.followup.send(
                embed=embeds[0], view=LyricsPaginator(embeds, interaction.user.id)
            )


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Lyrics(bot))
