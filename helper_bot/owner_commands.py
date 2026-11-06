"""
Every owner command lives here, on the helper bot.

Anything that has to DM people, post ads or leave servers is not done by this bot: it queues a job in the
shared database and the MAIN bot does it (see cogs/jobs.py), so users always hear from the main bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
from cogs.announce import DELAY_RANGE, MAX_LENGTH, announcement_embed
from cogs.backup import BACKUP_DIR, make_backup
from utils import format_duration, rel, stamp

log = logging.getLogger(__name__)

PLAN_INFO = {
    "weekly": ("Weekly", "7 days"),
    "monthly": ("Monthly", "30 days"),
    "lifetime": ("Lifetime", "never expires"),
}


# --- talking to the main bot ---------------------------------------------------

async def run_job(kind: str, payload: dict, timeout: float = 30) -> tuple[str, dict]:
    """
    Queue a job for the main bot and wait for it.
    Returns (status, result) with status: done | failed | offline | timeout.
    """
    if not await db.main_alive():
        return "offline", {}
    job_id = await db.create_job(kind, payload)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await db.get_job(job_id)
        if job["status"] in ("done", "failed"):
            return job["status"], json.loads(job["result"] or "{}")
        await asyncio.sleep(1)
    if await db.cancel_job_if_pending(job_id):
        return "offline", {}  # nobody picked it up, so make sure it can't run later by surprise
    return "timeout", {}  # it is running, just slow


def problem_embed(status: str, result: dict) -> discord.Embed:
    if status == "offline":
        return embeds.error("Main Bot Offline", "The main bot isn't running, so nothing was done. Start it and try again.")
    if status == "timeout":
        return embeds.warning("Still Working", "The main bot is still busy with this. It keeps going in the background.")
    return embeds.error("Main Bot Error", f"The main bot hit a problem: `{result.get('error', 'unknown error')}`")


async def send_dm(user_id: int, embed: discord.Embed) -> str | None:
    """DM someone through the main bot. None = delivered, otherwise a short reason it wasn't."""
    status, result = await run_job("dm", {"user_id": user_id, "embed": embed.to_dict()})
    if status != "done":
        return {"offline": "the main bot is offline", "timeout": "the main bot is too busy", "failed": result.get("error", "error")}[status]
    return None if result.get("ok") else result.get("reason", "unknown reason")


# --- shared UI -----------------------------------------------------------------

async def guild_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    rows = await db.search_bot_guilds(current)
    return [app_commands.Choice(name=f"{r['name']} ({r['guild_id']})"[:100], value=str(r["guild_id"])) for r in rows]


def parse_guild_id(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() else None


class ConfirmView(discord.ui.View):
    """Confirm / Cancel for destructive actions. `run` returns the embed to show afterwards."""

    def __init__(self, owner_id: int, label: str, run: Callable[[], Awaitable[discord.Embed]]) -> None:
        super().__init__(timeout=60)
        self.owner_id, self.run = owner_id, run
        self.interaction: discord.Interaction | None = None
        self.confirm.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.defer()
        await interaction.edit_original_response(embed=await self.run(), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=embeds.info("Cancelled", "Nothing was changed."), view=None)

    async def on_timeout(self) -> None:
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                embed=embeds.warning("Timed Out", "Nothing was changed."), view=None
            )
        except discord.HTTPException:
            pass


class AnnounceModal(discord.ui.Modal, title="Send an Announcement"):
    def __init__(self) -> None:
        super().__init__()
        self.text = discord.ui.TextInput(
            label="Message to DM to everyone",
            style=discord.TextStyle.paragraph,
            placeholder="Markdown works. Everyone who ran /setup will get this.",
            min_length=5,
            max_length=MAX_LENGTH,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        message = self.text.value.strip()
        count = len(await db.get_setup_users())
        if not count:
            await interaction.response.send_message(
                embed=embeds.info("Nobody To Message", "No one has run `/setup` yet."), ephemeral=True
            )
            return

        eta = format_duration(count * sum(DELAY_RANGE) / 2)
        confirm = embeds.warning(
            "Send This Announcement?",
            f"The main bot will DM **{count:,}** people who ran `/setup`, one every "
            f"{'-'.join(dict.fromkeys(str(d) for d in DELAY_RANGE))}s (about **{eta}**). Preview below.",
        )
        view = ConfirmAnnounce(interaction.user.id, message)
        await interaction.response.send_message(
            embeds=[confirm, announcement_embed(message)], view=view, ephemeral=True
        )
        view.interaction = interaction


class ConfirmAnnounce(discord.ui.View):
    def __init__(self, owner_id: int, message: str) -> None:
        super().__init__(timeout=180)
        self.owner_id, self.message = owner_id, message
        self.interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                embed=embeds.warning("Timed Out", "Nothing was sent. Run `/announce` again."), view=None
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Send to everyone", emoji="📨", style=discord.ButtonStyle.danger)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.info("Starting…", "Asking the main bot to start the announcement."), view=None
        )
        status, result = await run_job("announce", {"owner_id": self.owner_id, "message": self.message})
        if status != "done":
            await interaction.edit_original_response(embed=problem_embed(status, result))
            return
        await interaction.edit_original_response(
            embed=embeds.success(
                "Announcement Started",
                f"The main bot is sending to **{result['recipients']:,}** people, slowly. "
                "It will DM you a report when it finishes. Use `/announce-stop` to cancel.",
            )
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=embeds.info("Cancelled", "Nothing was sent."), view=None)


# --- the commands --------------------------------------------------------------

class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # premium codes ----------------------------------------------------------

    @app_commands.command(name="gen-code", description="Generate premium redeem codes (optionally DM them)")
    @app_commands.describe(
        plan="How long the premium lasts",
        amount="How many codes to make (1-10)",
        user="Optional: the main bot DMs the codes straight to this person",
    )
    @app_commands.choices(
        plan=[
            app_commands.Choice(name="Weekly (7 days)", value="weekly"),
            app_commands.Choice(name="Monthly (30 days)", value="monthly"),
            app_commands.Choice(name="Lifetime", value="lifetime"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    async def gen_code(
        self,
        interaction: discord.Interaction,
        plan: app_commands.Choice[str],
        amount: app_commands.Range[int, 1, 10] = 1,
        user: discord.User | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        codes = await db.create_codes(plan.value, amount, interaction.user.id)
        label, duration = PLAN_INFO[plan.value]
        log.info("%s generated %d %s code(s)", interaction.user.id, amount, plan.value)

        embed = embeds.success(
            "Codes Generated",
            f"**{amount}** {label.lower()} premium code{'s' if amount != 1 else ''} ready to hand out.",
        )
        embed.add_field(name="📦 Plan", value=label, inline=True)
        embed.add_field(name="⏳ Duration", value=duration, inline=True)
        embed.add_field(name="🔑 Uses", value="Single-use", inline=True)
        embed.add_field(name="🎟️ Codes", value="```\n" + "\n".join(codes) + "\n```", inline=False)
        embed.add_field(
            name="📬 How to redeem",
            value="The buyer runs `/redeem` on the **main bot** in their server and pastes the code.",
            inline=False,
        )

        if user is not None:
            dm = embeds.base(
                embeds.BRAND,
                "💎 Your Premium Code" if amount == 1 else "💎 Your Premium Codes",
                f"Here {'is' if amount == 1 else 'are'} your **{label.lower()}** premium "
                f"code{'s' if amount != 1 else ''} ({duration}). Premium promotes your server's ad "
                f"**{config.PREMIUM_BOOST}x more**.",
            )
            dm.add_field(
                name="🎟️ Code" if amount == 1 else "🎟️ Codes", value="```\n" + "\n".join(codes) + "\n```", inline=False
            )
            dm.add_field(
                name="📬 How to redeem",
                value="In your server, run `/redeem` on the **Partnership Bot** and paste the code.",
                inline=False,
            )
            problem = await send_dm(user.id, dm)
            embed.add_field(
                name="📨 Delivery",
                value=f"✅ Sent to {user.mention} by the main bot"
                if problem is None
                else f"⚠️ Couldn't DM {user.mention}: {problem}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # DMs and announcements (done by the main bot) ----------------------------

    @app_commands.command(name="dm", description="Send someone a DM (from the main bot)")
    @app_commands.describe(user="Who to message", message="What to say (max 1900 characters)")
    @app_commands.default_permissions(administrator=True)
    async def dm(
        self, interaction: discord.Interaction, user: discord.User, message: app_commands.Range[str, 1, 1900]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = embeds.base(embeds.BRAND, "📬 Message from the Partnership team", message)
        problem = await send_dm(user.id, embed)
        if problem is None:
            log.info("%s DMed %s via the main bot", interaction.user.id, user.id)
            result = embeds.success("DM Sent", f"The main bot delivered it to {user.mention}.")
            result.add_field(name="💬 Message", value=message[:1000], inline=False)
        else:
            result = embeds.error("Couldn't DM", f"Couldn't message {user.mention}: {problem}.")
        await interaction.followup.send(embed=result, ephemeral=True)

    @app_commands.command(name="announce", description="DM an announcement to everyone who ran /setup (via the main bot)")
    @app_commands.default_permissions(administrator=True)
    async def announce(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AnnounceModal())

    @app_commands.command(name="announce-stop", description="Stop an announcement that's being sent")
    @app_commands.default_permissions(administrator=True)
    async def announce_stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        status, result = await run_job("announce_stop", {})
        if status != "done":
            embed = problem_embed(status, result)
        elif result.get("stopped"):
            embed = embeds.success("Stopping", "It stops after the current DM. You'll get a report.")
        else:
            embed = embeds.info("Nothing Running", "No announcement is being sent.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="send-now", description="Post every ad to every partner server right now")
    @app_commands.default_permissions(administrator=True)
    async def send_now(self, interaction: discord.Interaction) -> None:
        participants = await db.get_participants()
        if len(participants) < 2:
            await interaction.response.send_message(
                embed=embeds.info(
                    "Nothing To Send",
                    "At least **2** servers need both a channel (`/setup`) and an ad (`/set-ad`).\n"
                    f"Currently participating: **{len(participants)}**.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=embeds.base(
                embeds.WARN,
                "📤 Sending All Ads…",
                f"The main bot is posting every ad to **{len(participants)}** servers. "
                "This takes a while. I'll update this message when it's done.",
            ),
            ephemeral=True,
        )
        started = time.perf_counter()
        status, result = await run_job("send_now", {}, timeout=14 * 60)
        if status != "done":
            await interaction.edit_original_response(embed=problem_embed(status, result))
            return

        done = embeds.success("All Ads Sent", "Every participating server received every partner ad.")
        done.add_field(name="🎯 Servers Reached", value=f"`{result['servers']:,}`", inline=True)
        done.add_field(name="📨 Ads Posted", value=f"`{result['sent']:,}`", inline=True)
        done.add_field(name="⏱️ Took", value=f"`{format_duration(time.perf_counter() - started)}`", inline=True)
        if result["failed"] or result["skipped"]:
            done.colour = embeds.WARN
            done.add_field(
                name="⚠️ Problems",
                value=(
                    f"Skipped **{result['skipped']}** server(s) (channel missing / no permission)\n"
                    f"Failed **{result['failed']}** post(s)"
                ),
                inline=False,
            )
        await interaction.edit_original_response(embed=done)

    # moderation ---------------------------------------------------------------

    @app_commands.command(name="reviews", description="Re-send every pending ad review to your DMs (from the main bot)")
    @app_commands.default_permissions(administrator=True)
    async def reviews(self, interaction: discord.Interaction) -> None:
        pending = await db.pending_reviews()
        if not pending:
            await interaction.response.send_message(
                embed=embeds.success("All Clear", "No ads are waiting for review."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        status, result = await run_job("resend_reviews", {}, timeout=len(pending) * 3 + 30)
        embed = (
            embeds.info("Pending Reviews", f"The main bot sent **{result['sent']}** of **{result['total']}** pending review(s) to your DMs.")
            if status == "done"
            else problem_embed(status, result)
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="unban", description="Let a banned user use the main bot again")
    @app_commands.describe(user="Who to unban")
    @app_commands.default_permissions(administrator=True)
    async def unban(self, interaction: discord.Interaction, user: discord.User) -> None:
        removed = await db.unban_user(user.id)
        embed = (
            embeds.success("Unbanned", f"{user.mention} can use the bot again.")
            if removed
            else embeds.info("Not Banned", f"{user.mention} isn't banned.")
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # servers --------------------------------------------------------------------

    @app_commands.command(name="server-info", description="Everything the bot knows about a server")
    @app_commands.describe(server="Server name or ID")
    @app_commands.autocomplete(server=guild_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def server_info(self, interaction: discord.Interaction, server: str) -> None:
        guild_id = parse_guild_id(server)
        if guild_id is None:
            await interaction.response.send_message(
                embed=embeds.error("Invalid Server", "Pick a server from the list, or paste its numeric ID."),
                ephemeral=True,
            )
            return

        snap = await db.get_bot_guild(guild_id)
        cfg = await db.get_guild(guild_id)
        ad = await db.get_ad(guild_id)
        pending = await db.pending_review_for_guild(guild_id)
        premium = await db.get_premium(guild_id)
        stats = await db.ad_stats(guild_id)
        received = await db.received_count(guild_id)
        banned = await db.get_banned_guild(guild_id)

        if not (snap or cfg or ad or banned or stats["total"] or received):
            await interaction.response.send_message(
                embed=embeds.error("Unknown Server", f"The bot has never seen server `{guild_id}`."), ephemeral=True
            )
            return

        if banned:
            status = f"🚫 **Banned** · {banned['reason'] or 'no reason given'}"
        elif snap:
            status = "🟢 The main bot is in this server"
        else:
            status = "⚪ The main bot is not in this server"

        embed = embeds.base(
            embeds.ERROR if banned else embeds.BRAND,
            f"🏠 {snap['name'] if snap else (banned['name'] if banned and banned['name'] else 'Unknown server')}",
            status,
        )
        if snap and snap["icon_url"]:
            embed.set_thumbnail(url=snap["icon_url"])
        embed.add_field(name="🆔 ID", value=f"`{guild_id}`", inline=True)
        if snap:
            embed.add_field(name="👑 Owner", value=f"<@{snap['owner_id']}>\n`{snap['owner_id']}`", inline=True)
            embed.add_field(name="👥 Members", value=f"`{snap['member_count'] or 0:,}`", inline=True)
            if snap["created_at"]:
                embed.add_field(name="📅 Created", value=rel(snap["created_at"]), inline=True)
            if snap["joined_at"]:
                embed.add_field(name="🤖 Bot joined", value=rel(snap["joined_at"]), inline=True)

        channel_id = cfg["channel_id"] if cfg else None
        embed.add_field(name="📍 Ad channel", value=f"<#{channel_id}>" if channel_id else "Not set", inline=True)
        embed.add_field(
            name="👤 Set up by",
            value=f"<@{cfg['setup_by']}>" if cfg and cfg["setup_by"] else "—",
            inline=True,
        )
        if ad:
            ad_text = f"✅ Set ({len(ad['content']):,} chars){' · ✔️ approved' if ad['approved'] else ''}"
        else:
            ad_text = "❌ None"
        if pending:
            ad_text += f"\n⏳ Review #{pending['id']} pending"
        embed.add_field(name="📝 Ad", value=ad_text, inline=True)

        if premium:
            expires = premium["expires_at"]
            premium_text = f"💎 {premium['plan'].title()} · " + ("never expires" if expires is None else f"ends {rel(expires)}")
        else:
            premium_text = "None"
        embed.add_field(name="💎 Premium", value=premium_text, inline=True)
        embed.add_field(
            name="📨 Their ad",
            value=f"Posted `{stats['total']:,}`× in `{stats['channels']:,}` channels"
            + (f"\nLast {rel(stats['last'])}" if stats["last"] else ""),
            inline=True,
        )
        embed.add_field(name="📥 Ads received", value=f"`{received:,}`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="kick-server", description="Make the main bot leave a server")
    @app_commands.describe(server="Server name or ID")
    @app_commands.autocomplete(server=guild_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def kick_server(self, interaction: discord.Interaction, server: str) -> None:
        guild_id = parse_guild_id(server)
        snap = await db.get_bot_guild(guild_id) if guild_id else None
        if snap is None:
            await interaction.response.send_message(
                embed=embeds.error("Not In Server", "The main bot isn't in that server."), ephemeral=True
            )
            return

        async def run() -> discord.Embed:
            status, result = await run_job("leave_guild", {"guild_id": guild_id})
            if status != "done":
                return problem_embed(status, result)
            if not result.get("left"):
                return embeds.error("Couldn't Leave", result.get("reason", "unknown reason"))
            return embeds.success(
                "Left Server",
                f"The main bot left **{result['name']}** and its ad / settings were removed. "
                "It can be invited back. Use `/ban-server` to block that.",
            )

        view = ConfirmView(interaction.user.id, "Leave server", run)
        await interaction.response.send_message(
            embed=embeds.warning(
                "Leave This Server?",
                f"The main bot will leave **{snap['name']}** (`{guild_id}`, {snap['member_count'] or 0:,} members). "
                "Its ad and settings are deleted.",
            ),
            view=view,
            ephemeral=True,
        )
        view.interaction = interaction

    @app_commands.command(name="ban-server", description="Ban a server: the main bot leaves and can't be re-added")
    @app_commands.describe(
        server="Server name or ID",
        reason="Why. This is sent to whoever set the bot up there if they try to add it again",
    )
    @app_commands.autocomplete(server=guild_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def ban_server(
        self, interaction: discord.Interaction, server: str, reason: app_commands.Range[str, 1, 200] = "No reason given"
    ) -> None:
        guild_id = parse_guild_id(server)
        if guild_id is None:
            await interaction.response.send_message(
                embed=embeds.error("Invalid Server", "Pick a server from the list, or paste its numeric ID."),
                ephemeral=True,
            )
            return
        snap = await db.get_bot_guild(guild_id)
        name = snap["name"] if snap else f"server {guild_id}"

        async def run() -> discord.Embed:
            cfg = await db.get_guild(guild_id)  # remember who ran /setup before the data is deleted
            await db.ban_guild(guild_id, name, reason, interaction.user.id, cfg["setup_by"] if cfg else None)
            await db.delete_guild_data(guild_id)
            left = ""
            if snap is not None:
                status, result = await run_job("leave_guild", {"guild_id": guild_id})
                left = (
                    " The main bot left the server."
                    if status == "done" and result.get("left")
                    else " ⚠️ The main bot couldn't leave right now. It will leave when it next starts or if re-added."
                )
            log.warning("Banned guild %s (%s): %s", name, guild_id, reason)
            return embeds.success(
                "Server Banned",
                f"**{name}** (`{guild_id}`) is banned: its ad and settings are deleted and the main bot "
                f"leaves right away if it is added again.{left}",
            )

        view = ConfirmView(interaction.user.id, "Ban server", run)
        await interaction.response.send_message(
            embed=embeds.warning(
                "Ban This Server?",
                f"**{name}** (`{guild_id}`) will be banned and its data deleted.\n**Reason:** {reason}",
            ),
            view=view,
            ephemeral=True,
        )
        view.interaction = interaction

    @app_commands.command(name="unban-server", description="Lift a server ban")
    @app_commands.describe(server="Server ID")
    @app_commands.default_permissions(administrator=True)
    async def unban_server(self, interaction: discord.Interaction, server: str) -> None:
        guild_id = parse_guild_id(server)
        if guild_id is None:
            await interaction.response.send_message(
                embed=embeds.error("Invalid Server", "Paste the server's numeric ID."), ephemeral=True
            )
            return
        removed = await db.unban_guild(guild_id)
        embed = (
            embeds.success("Server Unbanned", f"Server `{guild_id}` can add the main bot again.")
            if removed
            else embeds.info("Not Banned", f"Server `{guild_id}` isn't banned.")
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # database -------------------------------------------------------------------

    @app_commands.command(name="backup", description="Back up the database right now")
    @app_commands.default_permissions(administrator=True)
    async def backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            path = await asyncio.to_thread(make_backup)
        except Exception as exc:
            log.exception("Manual backup failed")
            await interaction.followup.send(embed=embeds.error("Backup Failed", f"`{exc}`"), ephemeral=True)
            return

        count = len(list(BACKUP_DIR.glob("bot-*.db")))
        embed = embeds.success("Backup Created", "A consistent copy of the database was saved.")
        embed.add_field(name="📄 File", value=f"`backups/{path.name}`", inline=False)
        embed.add_field(name="💾 Size", value=f"`{path.stat().st_size / 1024:.1f} KB`", inline=True)
        embed.add_field(name="🗂️ Kept", value=f"`{count}` (newest {config.BACKUP_KEEP})", inline=True)
        embed.add_field(name="⏱️ Automatic", value=f"every `{config.BACKUP_EVERY_HOURS}h`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
