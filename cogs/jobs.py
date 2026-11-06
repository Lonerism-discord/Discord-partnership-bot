"""
Job runner: executes work the helper bot (owner commands) queues in the shared database.
Everything that DMs people or touches servers happens here, as the main bot.

Job kinds: dm, announce, announce_stop, send_now, leave_guild, resend_reviews
"""
from __future__ import annotations

import asyncio
import json
import logging

import discord
from discord.ext import commands, tasks

import config
import db

log = logging.getLogger(__name__)


def guild_row(guild: discord.Guild) -> tuple:
    return (
        guild.id,
        guild.name,
        guild.owner_id,
        guild.member_count or 0,
        guild.icon.with_size(256).url if guild.icon else None,
        int(guild.created_at.timestamp()),
        int(guild.me.joined_at.timestamp()) if guild.me and guild.me.joined_at else None,
    )


class Jobs(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._running: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        await db.reset_stale_jobs()
        self.poll.start()
        self.sync_guilds.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()
        self.sync_guilds.cancel()

    # --- guild snapshot (the helper bot reads this to list / look up servers) ---

    @tasks.loop(minutes=10)
    async def sync_guilds(self) -> None:
        try:
            await db.sync_bot_guilds([guild_row(g) for g in self.bot.guilds])
        except Exception:
            log.exception("Guild snapshot failed")

    @sync_guilds.before_loop
    async def _before_sync(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await db.upsert_bot_guild(guild_row(guild))

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await db.remove_bot_guild(guild.id)

    # --- job loop -----------------------------------------------------------

    @tasks.loop(seconds=2)
    async def poll(self) -> None:
        try:
            await db.set_meta("main_heartbeat", str(db.now()))
            while (job := await db.claim_next_job()) is not None:
                task = asyncio.create_task(self.execute(job))  # own task: a long job never blocks others
                self._running.add(task)
                task.add_done_callback(self._running.discard)
        except Exception:
            log.exception("Job poll failed")

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def execute(self, job) -> None:
        handler = getattr(self, f"job_{job['kind']}", None)
        try:
            if handler is None:
                raise ValueError(f"unknown job kind {job['kind']!r}")
            result = await handler(json.loads(job["payload"]))
            await db.finish_job(job["id"], "done", result)
        except Exception as exc:
            log.exception("Job #%s (%s) failed", job["id"], job["kind"])
            await db.finish_job(job["id"], "failed", {"error": str(exc) or exc.__class__.__name__})

    # --- handlers -----------------------------------------------------------

    async def _user(self, user_id: int) -> discord.User:
        return self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)

    async def job_dm(self, p: dict) -> dict:
        """Send one user an embed from the main bot."""
        user = await self._user(p["user_id"])
        try:
            await user.send(embed=discord.Embed.from_dict(p["embed"]))
        except discord.Forbidden:
            return {"ok": False, "reason": "their DMs are closed (or they blocked the bot)"}
        except discord.HTTPException as exc:
            return {"ok": False, "reason": f"Discord error: {exc.text or exc.status}"}
        return {"ok": True}

    async def job_announce(self, p: dict) -> dict:
        announce = self.bot.get_cog("Announce")
        if announce.running:
            raise RuntimeError("an announcement is already being sent")
        recipients = await db.get_setup_users()
        owner = await self._user(p["owner_id"])
        announce.start(owner, p["message"], recipients)
        return {"started": True, "recipients": len(recipients)}

    async def job_announce_stop(self, p: dict) -> dict:
        announce = self.bot.get_cog("Announce")
        if not announce.running:
            return {"stopped": False}
        announce.stop_requested = True
        return {"stopped": True}

    async def job_send_now(self, p: dict) -> dict:
        return await self.bot.get_cog("Delivery").send_all()

    async def job_leave_guild(self, p: dict) -> dict:
        guild = self.bot.get_guild(p["guild_id"])
        if guild is None:
            return {"left": False, "reason": "the bot isn't in that server"}
        name = guild.name
        await guild.leave()
        log.warning("Left %s (%s) on the owner's command", name, p["guild_id"])
        return {"left": True, "name": name}

    async def job_resend_reviews(self, p: dict) -> dict:
        moderation = self.bot.get_cog("Moderation")
        pending = await db.pending_reviews()
        sent = 0
        for review in pending:
            if await moderation.notify_owners(review):
                sent += 1
            await asyncio.sleep(1)
        return {"sent": sent, "total": len(pending)}


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Jobs(bot))
