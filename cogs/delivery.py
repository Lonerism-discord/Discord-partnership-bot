from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands, tasks

import config
import db
import embeds
import moderation

log = logging.getLogger(__name__)


def pick_ad(candidates, now: float | None = None):
    """
    Pick the next ad for a server: the one most "overdue" for it.
    Overdue = time since this server last saw the ad, multiplied by PREMIUM_BOOST for premium
    advertisers — so premium ads come around ~3x as often as normal ones.
    """
    if not candidates:
        return None
    now = time.time() if now is None else now
    return max(
        candidates,
        key=lambda c: (now - (c["last_seen"] or 0)) * (config.PREMIUM_BOOST if c["premium"] else 1),
    )


class Delivery(commands.Cog):
    """Every minute: give each due server (channel + ad) one partner ad from another server."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.lock = asyncio.Lock()  # tick and /send-now never post at the same time

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # --- scheduled rotation -------------------------------------------------

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            async with self.lock:
                cutoff = int(time.time()) - config.AD_INTERVAL_MINUTES * 60
                for target in await db.get_due_guilds(cutoff):
                    guild = self.bot.get_guild(target["guild_id"])
                    if guild is None:
                        continue
                    try:
                        await self.deliver(guild, target)
                    except Exception:
                        log.exception("Delivery to guild %s failed", guild.id)
                        await db.mark_received(guild.id)  # back off until next interval
                    await asyncio.sleep(1)  # be gentle with rate limits
        except Exception:
            log.exception("Delivery tick crashed")

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(15)

    async def deliver(self, guild: discord.Guild, target) -> None:
        ad = pick_ad(await self.screened_candidates(guild.id))
        if ad is None:
            return  # nobody else to show yet — retried next tick

        channel = await self.resolve_channel(guild, target)
        if channel is None:
            await db.mark_received(guild.id)
            return

        await self.post_ad(guild, channel, ad)
        await db.mark_received(guild.id)  # also backs off until the next interval after a failed send

    # --- shared helpers -----------------------------------------------------

    async def screened_candidates(self, guild_id: int) -> list:
        """Ads this server may be shown. Last line of defence: unapproved ads are re-scanned here,
        and any that trip the filter are pulled from rotation and sent to the owner for review."""
        moderator = self.bot.get_cog("Moderation")
        clean = []
        for candidate in await db.get_candidate_ads(guild_id):
            if not self.bot.get_guild(candidate["guild_id"]):
                continue
            if not candidate["approved"]:
                flags = moderation.scan(candidate["content"])
                if flags:
                    if moderator is not None:
                        await moderator.hold_existing(candidate, flags)
                    continue
            clean.append(candidate)
        return clean

    async def resolve_channel(self, guild: discord.Guild, target) -> discord.TextChannel | None:
        """The configured ad channel if it still exists and we can post there, else None."""
        channel = guild.get_channel(target["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            log.info("Channel %s gone in guild %s, clearing", target["channel_id"], guild.id)
            await db.clear_channel(guild.id)
            return None

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            log.info("Missing perms in #%s (%s)", channel.name, guild.id)
            return None
        return channel

    async def post_ad(
        self, guild: discord.Guild, channel: discord.TextChannel, ad, *, feature: bool | None = None
    ) -> bool:
        """
        Post one ad into a channel and record it. True on success.

        Premium ads can be *featured*: an @everyone ping plus a pin (replacing our previous pin).
        feature=None  -> featured when the channel's cooldown allows (normal rotation)
        feature=True  -> featured regardless of cooldown (/send-now)
        feature=False -> never featured
        Each half only happens if the bot has the matching permission in that channel.
        """
        source = self.bot.get_guild(ad["guild_id"])
        if source is None:
            return False

        ping = pin = False
        if ad["premium"] and feature is not False:
            row = await db.get_featured(channel.id)
            cooled_down = row is None or time.time() - row["last_featured_at"] >= config.FEATURE_COOLDOWN_HOURS * 3600
            if feature is True or cooled_down:
                perms = channel.permissions_for(guild.me)
                ping = perms.mention_everyone
                pin = perms.manage_messages or getattr(perms, "pin_messages", False)

        view = embeds.ad_view(ad["content"], report_for=ad["guild_id"])
        try:
            message = await channel.send(
                content=embeds.ad_message(ad["content"], ping=ping),
                view=view,
                suppress_embeds=True,  # plain text only — no link-preview cards
                allowed_mentions=discord.AllowedMentions(everyone=True) if ping else discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            await db.clear_channel(guild.id)
            return False
        except discord.HTTPException as exc:
            log.warning("Send failed in guild %s: %s", guild.id, exc)
            return False
        finally:
            view.stop()  # buttons keep working (Report is a DynamicItem); this just frees the view

        await db.record_delivery(source.id, guild.id, guild.name, channel.id, message.id)
        if ping or pin:
            await self.pin_featured(channel, message, pin)
        return True

    async def pin_featured(self, channel: discord.TextChannel, message: discord.Message, pin: bool) -> None:
        """Pin a featured post (unpinning the one we pinned before) and start the channel's cooldown."""
        pinned_id = None
        if pin:
            row = await db.get_featured(channel.id)
            if row and row["pinned_message_id"]:
                try:
                    old = await channel.fetch_message(row["pinned_message_id"])
                    await old.unpin()
                except discord.HTTPException:
                    pass  # already gone / not pinned any more
            try:
                await message.pin()
                pinned_id = message.id
            except discord.HTTPException as exc:
                log.warning("Couldn't pin in #%s: %s", channel.name, exc)
        await db.set_featured(channel.id, pinned_id)

    async def send_all(self) -> dict:
        """Post every participating server's ad into every other participating server, right now."""
        result = {"servers": 0, "sent": 0, "failed": 0, "skipped": 0}
        async with self.lock:
            for target in await db.get_participants():
                guild = self.bot.get_guild(target["guild_id"])
                if guild is None:
                    continue
                channel = await self.resolve_channel(guild, target)
                if channel is None:
                    result["skipped"] += 1
                    continue

                ads = await self.screened_candidates(guild.id)
                # Premium ads go out last (so they're the newest messages); only the final one is featured.
                ads.sort(key=lambda a: bool(a["premium"]))
                last_premium = max((i for i, a in enumerate(ads) if a["premium"]), default=None)
                delivered = 0
                for index, ad in enumerate(ads):
                    if await self.post_ad(guild, channel, ad, feature=index == last_premium):
                        result["sent"] += 1
                        delivered += 1
                    else:
                        result["failed"] += 1
                    await asyncio.sleep(1)  # be gentle with rate limits
                if delivered:
                    result["servers"] += 1
                await db.mark_received(guild.id)
        return result


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Delivery(bot))
