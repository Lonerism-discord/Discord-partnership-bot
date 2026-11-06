from __future__ import annotations

import asyncio
import logging
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import embeds
from utils import rel, stamp

log = logging.getLogger(__name__)

PLAN_LABELS = {"weekly": "Weekly", "monthly": "Monthly", "lifetime": "Lifetime"}


def _owner_mention() -> str:
    return config.owner_mention()


def _perks() -> str:
    return (
        f"• 🚀 Your ad is promoted **{config.PREMIUM_BOOST}x more** than normal ads\n"
        "• 📌 Featured posts: **@everyone ping + pinned** in partner servers (where the bot is allowed)\n"
        "• ⏳ Reminders before your premium ends\n"
        "• 💎 Premium status shown in `/ad-status`\n"
        "• 🔁 Codes stack — redeem another to extend your time"
    )


# (seconds before expiry, wording) — largest first
REMINDER_STAGES = [(3 * 86400, "3 days"), (86400, "1 day"), (12 * 3600, "12 hours"), (3600, "1 hour")]


def next_reminder_stage(remaining: float, expires_at: int, notified_for: int | None, notified_stage: int | None):
    """
    Which reminder (threshold in seconds) is due now, or None.
    Sends each threshold once per expiry date. If the bot was offline and several thresholds passed,
    only the smallest one is sent. Extending premium changes expires_at, which resets the schedule.
    """
    due = [seconds for seconds, _ in REMINDER_STAGES if remaining <= seconds]
    if not due:
        return None
    smallest = min(due)
    if notified_for == expires_at and notified_stage is not None and notified_stage <= smallest:
        return None
    return smallest


class Premium(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminders.start()

    async def cog_unload(self) -> None:
        self.reminders.cancel()

    @tasks.loop(seconds=60)
    async def reminders(self) -> None:
        try:
            for row in await db.premium_expiring(REMINDER_STAGES[0][0]):
                remaining = row["expires_at"] - db.now()
                stage = next_reminder_stage(remaining, row["expires_at"], row["notified_for"], row["notified_stage"])
                if stage is None:
                    continue
                await db.mark_reminded(row["guild_id"], row["expires_at"], stage)  # first: never double-send
                await self.send_reminder(row, stage)
                await asyncio.sleep(1)
        except Exception:
            log.exception("Premium reminder loop crashed")

    @reminders.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    async def send_reminder(self, row, stage: int) -> None:
        """DM the person who activated premium (falling back to the server owner)."""
        wording = dict(REMINDER_STAGES)[stage]
        guild = self.bot.get_guild(row["guild_id"])
        name = guild.name if guild else f"server {row['guild_id']}"
        embed = embeds.base(
            embeds.WARN if stage > 3600 else embeds.ERROR,
            f"⏳ Your premium ends in {wording}",
            f"**{name}**'s premium (**{PLAN_LABELS[row['plan']]}**) ends {rel(row['expires_at'])}. "
            f"After that its ad goes back to normal promotion.",
        )
        embed.add_field(name="🕒 Ends", value=stamp(row["expires_at"]), inline=True)
        embed.add_field(
            name="🔁 Keep it going",
            value=f"DM {_owner_mention()} for a new code, then run `/redeem` — the time is added on top.",
            inline=False,
        )
        recipients = [row["activated_by"]]
        if guild and guild.owner_id not in recipients:
            recipients.append(guild.owner_id)
        for user_id in filter(None, recipients):
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
                log.info("Sent %s premium reminder for guild %s to %s", wording, row["guild_id"], user_id)
                return
            except discord.HTTPException as exc:
                log.warning("Premium reminder DM to %s failed: %s", user_id, exc)

    @app_commands.command(name="redeem", description="Redeem a premium code for this server")
    @app_commands.describe(code="Your premium code (looks like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def redeem(self, interaction: discord.Interaction, code: str) -> None:
        try:
            canonical = str(uuid.UUID(code.strip()))
        except ValueError:
            await interaction.response.send_message(
                embed=embeds.error(
                    "Invalid Code",
                    "That doesn't look like a premium code. Codes look like "
                    "`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.\n"
                    f"Need one? DM {_owner_mention()}.",
                ),
                ephemeral=True,
            )
            return

        status, info = await db.redeem_code(canonical, interaction.guild_id, interaction.user.id)

        if status == "invalid":
            embed = embeds.error("Code Not Found", f"That code doesn't exist. Need one? DM {_owner_mention()}.")
        elif status == "used":
            embed = embeds.error("Code Already Used", "Someone already redeemed that code.")
        elif status == "lifetime":
            embed = embeds.info(
                "Already Lifetime", "This server already has **lifetime** premium — your code was **not** used."
            )
        else:
            plan = PLAN_LABELS[info["plan"]]
            expires = info["expires_at"]
            embed = embeds.base(
                embeds.BRAND,
                "💎 Premium Activated!",
                f"**{interaction.guild.name}** now has **{plan}** premium"
                f"{' (time added to your existing premium)' if info['extended'] else ''}.",
            )
            embed.add_field(name="📦 Plan", value=plan, inline=True)
            embed.add_field(
                name="⏳ Expires",
                value="Never — lifetime 🎉" if expires is None else f"{stamp(expires)}\n({rel(expires)})",
                inline=True,
            )
            embed.add_field(name="✨ Perks", value=_perks(), inline=False)
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.with_size(256).url)
            await interaction.response.send_message(embed=embed)
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="premium", description="See this server's premium status and how to get it")
    @app_commands.guild_only()
    async def premium(self, interaction: discord.Interaction) -> None:
        row = await db.get_premium(interaction.guild_id)
        if row:
            expires = row["expires_at"]
            embed = embeds.base(embeds.BRAND, "💎 Premium Active", "This server's ad gets boosted promotion.")
            embed.add_field(name="📦 Plan", value=PLAN_LABELS[row["plan"]], inline=True)
            embed.add_field(
                name="⏳ Expires",
                value="Never — lifetime 🎉" if expires is None else f"{stamp(expires)}\n({rel(expires)})",
                inline=True,
            )
            embed.add_field(name="✨ Perks", value=_perks(), inline=False)
        else:
            embed = embeds.base(
                embeds.WARN,
                "💎 Get Premium",
                f"Premium promotes your server's ad **{config.PREMIUM_BOOST}x more** than normal ads.",
            )
            embed.add_field(name="✨ Perks", value=_perks(), inline=False)
            embed.add_field(
                name="🛒 How to get it",
                value=(
                    f"**1.** DM {_owner_mention()} to get a code (weekly, monthly or lifetime)\n"
                    "**2.** Run `/redeem` and paste your code"
                ),
                inline=False,
            )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.with_size(256).url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Premium(bot))
