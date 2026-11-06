"""
Mass-DM engine for announcements. The owner starts it from the helper bot (/announce);
the helper queues a job and this cog does the actual DMing, so people get the DM from the main bot.
"""
from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord.ext import commands

import embeds

log = logging.getLogger(__name__)

DELAY_RANGE = (2, 2)  # seconds between DMs (min, max)
SPAM_PAUSE = 300  # seconds to back off if Discord says we're opening DMs too fast
MAX_LENGTH = 1900


def announcement_embed(message: str) -> discord.Embed:
    embed = embeds.base(embeds.BRAND, "📢 Announcement", message)
    embed.set_footer(text="Partnership Bot  •  You got this because you set the bot up in a server")
    return embed


class Announce(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.task: asyncio.Task | None = None
        self.stop_requested = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, owner: discord.abc.User, message: str, recipients: list[int]) -> bool:
        if self.running:
            return False
        self.stop_requested = False
        self.task = asyncio.create_task(self.run(owner, message, recipients))
        return True

    async def send_one(self, user_id: int, embed: discord.Embed) -> str:
        """DM one user. Returns sent | closed | failed | spam."""
        for attempt in range(2):
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
                return "sent"
            except discord.Forbidden:
                return "closed"  # DMs closed / blocked the bot
            except discord.NotFound:
                return "failed"
            except discord.HTTPException as exc:
                if exc.code == 40003:  # "You are opening direct messages too fast"
                    if attempt == 0:
                        log.warning("Discord says DMs too fast - pausing %ss", SPAM_PAUSE)
                        await asyncio.sleep(SPAM_PAUSE)
                        continue
                    return "spam"
                log.warning("DM to %s failed: %s", user_id, exc)
                return "failed"
            except Exception:
                log.exception("DM to %s crashed", user_id)
                return "failed"
        return "spam"

    async def run(self, owner: discord.abc.User, message: str, recipients: list[int]) -> dict:
        embed = announcement_embed(message)
        stats = {"sent": 0, "closed": 0, "failed": 0}
        aborted: str | None = None
        log.info("Announcement started: %d recipients", len(recipients))

        for index, user_id in enumerate(recipients):
            if self.stop_requested:
                aborted = "stopped by you"
                break
            outcome = await self.send_one(user_id, embed)
            if outcome == "spam":
                aborted = "Discord flagged the sending as too fast, so I stopped to protect the bot"
                break
            stats[outcome] += 1
            if index < len(recipients) - 1:
                await asyncio.sleep(random.uniform(*DELAY_RANGE))

        handled = sum(stats.values())
        report = (
            embeds.success("Announcement Finished", f"Delivered to **{stats['sent']:,}** of **{len(recipients):,}** people.")
            if aborted is None
            else embeds.warning("Announcement Stopped Early", f"Stopped: {aborted}.")
        )
        report.add_field(name="✅ Sent", value=f"`{stats['sent']:,}`", inline=True)
        report.add_field(name="🔒 DMs Closed", value=f"`{stats['closed']:,}`", inline=True)
        report.add_field(name="❌ Failed", value=f"`{stats['failed']:,}`", inline=True)
        if aborted:
            report.add_field(name="⏭️ Not Reached", value=f"`{len(recipients) - handled:,}`", inline=True)
        log.info("Announcement done: %s aborted=%s", stats, aborted)
        try:
            await owner.send(embed=report)
        except discord.HTTPException:
            log.warning("Couldn't DM the announcement report to the owner")
        return stats

    async def cog_unload(self) -> None:
        if self.running:
            self.task.cancel()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Announce(bot))
