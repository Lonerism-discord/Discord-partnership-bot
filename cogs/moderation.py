from __future__ import annotations

import asyncio
import json
import logging

import discord
from discord.ext import commands

import config
import db
import embeds
from moderation import CATEGORY_LABELS, Flag
from utils import rel

log = logging.getLogger(__name__)

SEVERITY_ICON = {"high": "🔴", "medium": "🟠"}


def _flags(review) -> list[Flag]:
    return [Flag(**f) for f in json.loads(review["flags"])]


def review_embed(review, outcome: str | None = None, colour: discord.Colour | None = None) -> discord.Embed:
    flags = _flags(review)
    high = any(f.severity == "high" for f in flags)
    if outcome is None and review["kind"] == "report":
        embed = embeds.base(
            embeds.WARN,
            "🚩 Ad Reported",
            "A user reported an ad that is **currently live** in partner servers. "
            "Nothing changes unless you act.",
        )
    elif outcome is None:
        embed = embeds.base(
            embeds.ERROR if high else embeds.WARN,
            "🚨 Ad Held For Review",
            "This ad tripped the content filter. **It has NOT been posted anywhere.** "
            "The submitter was shown a normal \"saved\" message and doesn't know it's held.",
        )
    else:
        embed = embeds.base(colour or embeds.INFO, f"🚨 Review #{review['id']} — {outcome}")

    embed.add_field(
        name="🏠 Server", value=f"**{review['guild_name'] or 'Unknown'}**\n`{review['guild_id']}`", inline=True
    )
    embed.add_field(
        name="👤 Submitted by",
        value=f"<@{review['user_id']}>\n`{review['user_name'] or 'unknown'}` · `{review['user_id']}`",
        inline=True,
    )
    embed.add_field(name="🕒 When", value=rel(review["created_at"]), inline=True)

    lines = []
    for f in flags[:8]:
        label = CATEGORY_LABELS.get(f.category, f.category)
        lines.append(f"{SEVERITY_ICON.get(f.severity, '•')} **{f.severity.title()}** · {label}\n> `{f.match}` — {f.reason}")
    embed.add_field(name="🚩 Why it was flagged", value="\n".join(lines)[:1024] or "—", inline=False)

    text = review["content"].replace("```", "'''")
    shown = text if len(text) <= 900 else text[:900] + "…"
    embed.add_field(name="📝 Ad text", value=f"```\n{shown}\n```", inline=False)
    embed.set_footer(text=f"{embeds.FOOTER}  •  Review #{review['id']}")
    return embed


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"review:(?P<action>accept|decline):(?P<id>\d+)",
):
    """Accept / Decline buttons that keep working after a bot restart."""

    def __init__(self, action: str, review_id: int, kind: str = "filter") -> None:
        accept = action == "accept"
        if kind == "report":
            label = "Dismiss report" if accept else "Remove ad & ban advertiser"
        else:
            label = "Accept — post it" if accept else "Decline & ban user"
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji="✅" if accept else "⛔",
                style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                custom_id=f"review:{action}:{review_id}",
            )
        )
        self.action, self.review_id = action, review_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> "ReviewButton":
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in config.OWNER_IDS:
            return True
        await interaction.response.send_message(
            embed=embeds.error("Owner Only", "Only the bot owner can decide reviews."), ephemeral=True
        )
        return False

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Moderation = interaction.client.get_cog("Moderation")
        await cog.decide(interaction, self.review_id, self.action)


def review_view(review_id: int, kind: str = "filter") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton("accept", review_id, kind))
    view.add_item(ReviewButton("decline", review_id, kind))
    return view


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ReviewButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(ReviewButton)

    # --- taking down already-posted copies ----------------------------------

    async def takedown(self, ad_guild_id: int) -> int:
        """Delete every copy of this server's ad that the bot posted in other servers. Returns the count."""
        removed = 0
        try:
            for row in await db.deliveries_for_ad(ad_guild_id):
                channel = self.bot.get_channel(row["target_channel_id"])
                if channel is None:
                    continue
                try:
                    await channel.get_partial_message(row["message_id"]).delete()
                    removed += 1
                except discord.HTTPException:
                    pass  # already deleted, or the channel is gone
                await asyncio.sleep(0.5)  # gentle on rate limits
        except Exception:
            log.exception("Takedown for guild %s crashed", ad_guild_id)
        log.warning("Took down %d posted cop%s of guild %s's ad", removed, "y" if removed == 1 else "ies", ad_guild_id)
        return removed

    def start_takedown(self, ad_guild_id: int) -> None:
        task = asyncio.create_task(self.takedown(ad_guild_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- holding content ----------------------------------------------------

    async def notify_owners(self, review) -> bool:
        """DM the review (with Accept / Decline) to every owner. True if at least one DM landed."""
        delivered = False
        for owner_id in config.OWNER_IDS:
            try:
                owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                await owner.send(embed=review_embed(review), view=review_view(review["id"], review["kind"]))
                delivered = True
            except discord.HTTPException as exc:
                log.warning("Couldn't DM review #%s to owner %s: %s", review["id"], owner_id, exc)
        return delivered

    async def hold(self, *, guild: discord.Guild, user: discord.abc.User, content: str, flags: list[Flag]) -> int:
        """Park a flagged ad for review instead of saving it."""
        review_id, created = await db.create_review(
            guild.id,
            guild.name,
            user.id,
            str(user),
            content,
            json.dumps([f.to_dict() for f in flags]),
        )
        log.warning("Ad from guild %s (user %s) held as review #%s", guild.id, user.id, review_id)
        if created:
            await self.notify_owners(await db.get_review(review_id))
        return review_id

    async def hold_report(
        self,
        *,
        reporter: discord.abc.User,
        reporter_guild: discord.Guild | None,
        advertiser_guild_id: int,
        content: str,
        reason: str,
        jump_url: str,
    ) -> int:
        """A user reported a live ad: queue it for the owner. The ad stays live until the owner acts."""
        ad = await db.get_ad(advertiser_guild_id)
        author_id = ad["updated_by"] if ad and ad["updated_by"] else 0
        author = self.bot.get_user(author_id) if author_id else None
        guild = self.bot.get_guild(advertiser_guild_id)
        where = f" in {reporter_guild.name}" if reporter_guild else ""
        flag = Flag("report", "medium", reason[:200], f"reported by {reporter} ({reporter.id}){where} — {jump_url}")
        review_id, created = await db.create_review(
            advertiser_guild_id,
            guild.name if guild else "Unknown",
            author_id,
            str(author) if author else "unknown",
            content,
            json.dumps([flag.to_dict()]),
            kind="report",
        )
        log.warning("Ad of guild %s reported by %s -> review #%s", advertiser_guild_id, reporter.id, review_id)
        if created:
            await self.notify_owners(await db.get_review(review_id))
        return review_id

    async def hold_existing(self, ad, flags: list[Flag]) -> None:
        """An already-live ad tripped the filter: pull it from rotation and queue it for review."""
        guild = self.bot.get_guild(ad["guild_id"])
        user_id = ad["updated_by"] or 0
        user = self.bot.get_user(user_id)
        await db.remove_ad(ad["guild_id"])
        self.start_takedown(ad["guild_id"])  # delete copies that already went out
        review_id, created = await db.create_review(
            ad["guild_id"],
            guild.name if guild else "Unknown",
            user_id,
            str(user) if user else "unknown",
            ad["content"],
            json.dumps([f.to_dict() for f in flags]),
        )
        log.warning("Live ad of guild %s pulled and held as review #%s", ad["guild_id"], review_id)
        if created:
            await self.notify_owners(await db.get_review(review_id))

    # --- owner decisions ----------------------------------------------------

    async def decide(self, interaction: discord.Interaction, review_id: int, action: str) -> None:
        review = await db.get_review(review_id)
        if review is None or review["status"] != "pending":
            status = review["status"] if review else "missing"
            await interaction.response.send_message(
                embed=embeds.info("Already Handled", f"Review #{review_id} is already **{status}**."), ephemeral=True
            )
            return

        is_report = review["kind"] == "report"

        if action == "accept":
            if not await db.decide_review(review_id, "accepted", interaction.user.id):
                return
            if is_report:  # report dismissed: the live ad is simply left alone
                outcome = "Dismissed ✅ — ad stays live"
            else:
                await db.set_ad(review["guild_id"], review["content"], review["user_id"], approved=True)
                outcome = "Accepted ✅ — ad is now live"
            updated = await db.get_review(review_id)
            await interaction.response.edit_message(
                embed=review_embed(updated, outcome, embeds.SUCCESS), view=None
            )
            return

        # decline: ban the user, pull the server's ad, tell the user
        await interaction.response.defer()
        if not await db.decide_review(review_id, "declined", interaction.user.id):
            return
        has_user = bool(review["user_id"])
        if has_user:
            await db.ban_user(review["user_id"], f"Declined review #{review_id}", interaction.user.id)
        await db.remove_ad(review["guild_id"])
        self.start_takedown(review["guild_id"])  # delete every posted copy of this server's ad
        notified = await self.send_ban_notice(review["user_id"]) if has_user else False
        updated = await db.get_review(review_id)
        embed = review_embed(updated, "Declined ⛔ — user banned" if has_user else "Declined ⛔ — ad removed", embeds.ERROR)
        if not has_user:
            result = "The server's ad was removed. The author is unknown, so nobody was banned."
        elif notified:
            result = "User banned from the bot and the server's ad removed.\nUser was sent both notices."
        else:
            result = "User banned from the bot and the server's ad removed.\n⚠️ Couldn't DM the user (their DMs are closed)."
        result += "\nPosted copies of the ad are being deleted from partner servers."
        embed.add_field(name="📨 Outcome", value=result, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)

    async def send_ban_notice(self, user_id: int) -> bool:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(
                embed=embeds.base(embeds.ERROR, "🚫 Service Ended", "We're no longer offering services for this account.")
            )
            await asyncio.sleep(1)
            await user.send(
                embed=embeds.base(embeds.ERROR, "⚖️ Terms of Service", "You've broken our Terms of Service.")
            )
            return True
        except discord.HTTPException as exc:
            log.warning("Couldn't DM ban notice to %s: %s", user_id, exc)
            return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
