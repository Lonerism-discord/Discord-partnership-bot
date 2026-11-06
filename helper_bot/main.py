"""
Helper bot: ALL owner/admin commands live here. Only the owner (OWNER_IDS) can use any of them.

It shares the main bot's SQLite database. For anything that must be done by the main bot (DMing people,
announcements, posting ads, leaving servers) it queues a job and the main bot runs it (cogs/jobs.py).

    python helper_bot/main.py        (or python start.py to run both bots)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Reuse the main bot's config / database / embed modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import embeds  # noqa: E402

log = logging.getLogger("helper-bot")


class OwnerTree(app_commands.CommandTree):
    """Nobody but the owner may run any command on this bot."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in config.OWNER_IDS:
            return True
        await interaction.response.send_message(
            embed=embeds.error("Owner Only", "This bot's commands are restricted to the bot owner."), ephemeral=True
        )
        return False


class HelperBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            tree_cls=OwnerTree,
        )
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self) -> None:
        await db.init()
        await self.load_extension("owner_commands")
        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await super().close()
        await db.close()

    async def on_ready(self) -> None:
        log.info("Helper logged in as %s (%s)", self.user, self.user.id)
        if not await db.main_alive():
            log.warning("The main bot doesn't look like it is running (no heartbeat yet)")

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Unhandled command error", exc_info=error)
        embed = embeds.error("Something Went Wrong", "An unexpected error occurred. Please try again.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    if not config.HELPER_TOKEN:
        sys.exit("HELPER_DISCORD_TOKEN missing. Create a second bot, then add its token to .env.")
    discord.utils.setup_logging()
    HelperBot().run(config.HELPER_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
