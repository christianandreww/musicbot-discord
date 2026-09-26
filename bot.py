"""Entry point.  Run with:  python bot.py   (start Lavalink first — see README)"""
from __future__ import annotations

import asyncio
import logging

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("musicbot")

EXTENSIONS = ("cogs.music", "cogs.lyrics")


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        # Slash-command-only: no privileged intents needed. Voice states are
        # part of the defaults, which is all we use.
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self) -> None:
        node = wavelink.Node(uri=config.LAVALINK_URI, password=config.LAVALINK_PASSWORD)
        # Wavelink reconnects automatically if the node drops.
        await wavelink.Pool.connect(nodes=[node], client=self, cache_capacity=100)

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        self.tree.on_error = self.on_app_command_error

        # Sync the command tree exactly once, at startup. NEVER sync in
        # on_ready: it can fire repeatedly and syncing is rate limited.
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # instant in your server
            # Wipe any GLOBAL registrations left over from earlier syncs, so
            # stale commands (like an old /playlist) and duplicates disappear.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()  # global: can take up to an hour to appear

    async def on_ready(self) -> None:
        log.info("Logged in as %s — ready.", self.user)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        log.exception("Command error", exc_info=original)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("😵 Something went wrong.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "😵 Something went wrong.", ephemeral=True
                )
        except discord.HTTPException:
            pass  # Interaction expired — nothing sensible left to do.


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    bot = MusicBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
