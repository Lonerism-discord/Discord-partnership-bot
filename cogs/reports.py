"""Report button under every posted partner ad (works across restarts via a DynamicItem)."""
from __future__ import annotations

import time

import discord
from discord.ext import commands

import db
import embeds

COOLDOWN_SECONDS = 60
_last_report: dict[int, float] = {}


class ReportModal(discord.ui.Modal, title="Report This Ad"):
    def __init__(self, advertiser_guild_id: int, ad_text: str, jump_url: str) -> None:
        super().__init__()
        self.advertiser_guild_id, self.ad_text, self.jump_url = advertiser_guild_id, ad_text, jump_url
        self.reason = discord.ui.TextInput(
            label="What's wrong with this ad?",
            style=discord.TextStyle.paragraph,
            placeholder="e.g. illegal content, scam, underage content, spam…",
            min_length=3,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.client.get_cog("Moderation").hold_report(
            reporter=interaction.user,
            reporter_guild=interaction.guild,
            advertiser_guild_id=self.advertiser_guild_id,
            content=self.ad_text,
            reason=self.reason.value.strip(),
            jump_url=self.jump_url,
        )
        await interaction.response.send_message(
            embed=embeds.success("Report Sent", "Thanks — the team will look at this ad."), ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        import logging

        logging.getLogger(__name__).exception("report modal failed", exc_info=error)
        message = embeds.error("Something Went Wrong", "Your report wasn't sent. Please try again.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=message, ephemeral=True)
        else:
            await interaction.response.send_message(embed=message, ephemeral=True)


class ReportButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"report:(?P<gid>\d+)",
):
    def __init__(self, advertiser_guild_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Report",
                emoji="🚩",
                style=discord.ButtonStyle.secondary,
                custom_id=f"report:{advertiser_guild_id}",
            )
        )
        self.advertiser_guild_id = advertiser_guild_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> "ReportButton":
        return cls(int(match["gid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if await db.is_banned(interaction.user.id):
            await interaction.response.send_message(
                embed=embeds.error("Service Ended", "We're no longer offering services for this account."),
                ephemeral=True,
            )
            return
        now = time.monotonic()
        if now - _last_report.get(interaction.user.id, -COOLDOWN_SECONDS) < COOLDOWN_SECONDS:
            await interaction.response.send_message(
                embed=embeds.info("Slow Down", "You just sent a report. Please wait a minute."), ephemeral=True
            )
            return
        _last_report[interaction.user.id] = now
        await interaction.response.send_modal(
            ReportModal(
                self.advertiser_guild_id,
                embeds.extract_ad(interaction.message.content),
                interaction.message.jump_url,
            )
        )


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(ReportButton)


async def teardown(bot: commands.Bot) -> None:
    bot.remove_dynamic_items(ReportButton)
