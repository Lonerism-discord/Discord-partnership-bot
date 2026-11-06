from __future__ import annotations

import logging
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
from utils import BASIC_PERMISSIONS, RESTORE_PERMISSIONS, oauth_link

log = logging.getLogger("partnership-bot")

EXTENSIONS = (
    "cogs.setup_channel",
    "cogs.ads",
    "cogs.general",
    "cogs.delivery",
    "cogs.premium",
    "cogs.announce",
    "cogs.moderation",
    "cogs.reports",
    "cogs.backup",
    "cogs.jobs",  # runs the owner commands queued by the helper bot
    "cogs.restore",  # /fix
)


class GuardedTree(app_commands.CommandTree):
    """Banned users can't run any command (the owner is never blocked)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in config.OWNER_IDS or not await db.is_banned(interaction.user.id):
            return True
        await interaction.response.send_message(
            embed=embeds.error("Service Ended", "We're no longer offering services for this account."),
            ephemeral=True,
        )
        return False


class PartnershipBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),  # no privileged intents needed
            help_command=None,
            tree_cls=GuardedTree,
        )
        self.started_at = time.time()
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self) -> None:
        await db.init()
        if not config.OWNER_IDS:
            log.warning("OWNER_IDS is empty: owner commands, ad reviews and premium contact info are disabled")

        # Invite links not set in .env are built from this bot's own application ID.
        if self.application_id:
            config.INVITE_URL = config.INVITE_URL or oauth_link(self.application_id, BASIC_PERMISSIONS)
            config.RESTORE_INVITE_URL = config.RESTORE_INVITE_URL or oauth_link(
                self.application_id, RESTORE_PERMISSIONS
            )

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", config.DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    async def close(self) -> None:
        await super().close()
        await db.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))
        for guild_id in await db.banned_guild_ids():  # leave any banned server we're still in
            guild = self.get_guild(guild_id)
            if guild is not None:
                log.warning("In banned guild %s (%s) - leaving", guild.name, guild_id)
                await guild.leave()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        banned = await db.get_banned_guild(guild.id)
        if banned:
            log.warning("Added to banned guild %s (%s) - telling them and leaving", guild.name, guild.id)
            await self.tell_banned_guild(guild, banned)
            await guild.leave()

    async def tell_banned_guild(self, guild: discord.Guild, banned) -> None:
        """DM the person who ran /setup there before (or the server owner) that the server is banned, and why."""
        embed = embeds.base(
            embeds.ERROR,
            "This server is banned",
            f"Your server {guild.name} is banned from using Partnership Bot, so I left it. "
            "If you think this is a mistake, join the support server and ask.",
        )
        embed.add_field(name="Reason", value=banned["reason"] or "No reason given", inline=False)
        kwargs = {"view": embeds.link_view(("Support Server", config.SUPPORT_URL, None))} if config.SUPPORT_URL else {}

        for user_id in dict.fromkeys(filter(None, [banned["setup_by"], guild.owner_id])):
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                await user.send(embed=embed, **kwargs)
                log.info("Told %s that guild %s is banned", user_id, guild.id)
                return
            except discord.HTTPException as exc:
                log.warning("Couldn't DM ban notice to %s: %s", user_id, exc)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await db.delete_guild_data(guild.id)
        log.info("Removed from %s (%s), data cleared", guild.name, guild.id)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            embed = embeds.error("Missing Permissions", "You need the **Manage Server** permission to use this.")
        elif isinstance(error, app_commands.NoPrivateMessage):
            embed = embeds.error("Server Only", "This command can only be used inside a server.")
        else:
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
    if not config.TOKEN:
        sys.exit("DISCORD_TOKEN missing. Copy .env.example to .env and fill it in.")
    # Root-level logging so our own loggers (cogs, delivery) show up, not just discord.py's.
    discord.utils.setup_logging()
    PartnershipBot().run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
