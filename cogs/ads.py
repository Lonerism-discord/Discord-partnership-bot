from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
import moderation
from utils import find_invite, format_minutes, rel, truncate

log = logging.getLogger(__name__)

MAX_AD_LENGTH = embeds.MAX_AD_LENGTH  # ad + template must fit one 2000-char message
MIN_AD_LENGTH = 20


async def current_ad(guild_id: int):
    """The ad the user believes they have: a submission awaiting review wins over the live one."""
    return await db.pending_review_for_guild(guild_id) or await db.get_ad(guild_id)


class AdModal(discord.ui.Modal, title="Save Your Server Ad"):
    def __init__(self, existing: str | None = None) -> None:
        super().__init__()
        self.ad = discord.ui.TextInput(
            label="Paste your server ad (as is)",
            style=discord.TextStyle.paragraph,
            placeholder="Paste your full advertisement here — markdown, emojis and invite link included.",
            default=existing[:MAX_AD_LENGTH] if existing else None,
            min_length=MIN_AD_LENGTH,
            max_length=MAX_AD_LENGTH,
            required=True,
        )
        self.add_item(self.ad)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        content = self.ad.value.strip()
        if len(content) < MIN_AD_LENGTH:
            await interaction.response.send_message(
                embed=embeds.error("Ad Too Short", f"Your ad needs at least **{MIN_AD_LENGTH}** characters."),
                ephemeral=True,
            )
            return

        if interaction.user.id not in config.OWNER_IDS and await db.is_banned(interaction.user.id):
            await interaction.response.send_message(
                embed=embeds.error("Service Ended", "We're no longer offering services for this account."),
                ephemeral=True,
            )
            return

        # Flagged ads are held for the owner's review but the user sees the normal "saved" flow.
        flags = moderation.scan(content)
        if flags:
            await interaction.client.get_cog("Moderation").hold(
                guild=interaction.guild, user=interaction.user, content=content, flags=flags
            )
        else:
            await db.set_ad(interaction.guild_id, content, interaction.user.id)
        cfg = await db.get_guild(interaction.guild_id)
        channel_id = cfg["channel_id"] if cfg else None
        invite = find_invite(content)

        saved = embeds.success("Ad Saved", "Your ad is now in the rotation. Preview of the post is below.")
        saved.add_field(name="📏 Length", value=f"`{len(content):,}` / `{MAX_AD_LENGTH:,}`", inline=True)
        saved.add_field(
            name="🔗 Invite Link",
            value="✅ Detected — a **Join Partnered Server** button is added" if invite else "⚠️ None found",
            inline=True,
        )
        saved.add_field(
            name="📍 Ad Channel",
            value=f"<#{channel_id}>" if channel_id else "❌ Not set",
            inline=True,
        )
        if not channel_id:
            saved.add_field(
                name="⚠️ One more step",
                value="Run `/setup` to pick a channel — your ad won't be shown anywhere until you do.",
                inline=False,
            )
        elif not invite:
            saved.add_field(
                name="💡 Tip",
                value="Add a `discord.gg/...` invite to your ad so people can join with one click.",
                inline=False,
            )

        await interaction.response.send_message(embed=saved, ephemeral=True)
        await interaction.followup.send(
            content=embeds.ad_message(content),
            view=embeds.ad_view(content),
            suppress_embeds=True,
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("set-ad modal failed", exc_info=error)
        message = embeds.error("Something Went Wrong", "Your ad wasn't saved. Please try again.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=message, ephemeral=True)
        else:
            await interaction.response.send_message(embed=message, ephemeral=True)


class ConfirmRemove(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=30)
        self.interaction: discord.Interaction | None = None

    @discord.ui.button(label="Remove Ad", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        removed = await db.remove_ad(interaction.guild_id) or (
            await db.pending_review_for_guild(interaction.guild_id) is not None
        )
        stats = await db.ad_stats(interaction.guild_id)
        self.stop()
        if not removed:
            embed = embeds.info("Nothing To Remove", "Your server doesn't have an ad set.")
        else:
            embed = embeds.success("Ad Removed", "Your ad is out of the rotation and won't be posted anymore.")
            embed.add_field(
                name="📊 Final Numbers",
                value=f"Posted **{stats['total']:,}** times across **{stats['channels']:,}** channels.",
                inline=False,
            )
            embed.add_field(
                name="♻️ Come back anytime",
                value="Use `/set-ad` to add a new ad. Your stats are kept.",
                inline=False,
            )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.info("Cancelled", "Your ad was left untouched."), view=None
        )

    async def on_timeout(self) -> None:
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                embed=embeds.warning("Timed Out", "No changes were made to your ad."), view=None
            )
        except discord.HTTPException:
            pass


class Ads(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="set-ad", description="Open a text box to paste and save your server's ad")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_ad(self, interaction: discord.Interaction) -> None:
        existing = await current_ad(interaction.guild_id)
        await interaction.response.send_modal(AdModal(existing["content"] if existing else None))

    @app_commands.command(name="remove-ad", description="Remove your server's ad from the rotation")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_ad(self, interaction: discord.Interaction) -> None:
        ad = await current_ad(interaction.guild_id)
        if not ad:
            await interaction.response.send_message(
                embed=embeds.error("No Ad Found", "Your server doesn't have an ad yet. Use `/set-ad` to add one."),
                ephemeral=True,
            )
            return

        warn = embeds.warning("Remove Your Ad?", "Your ad will stop being posted in partner servers.")
        warn.add_field(name="📝 Current ad", value=f">>> {truncate(ad['content'], 400)}", inline=False)
        view = ConfirmRemove()
        await interaction.response.send_message(embed=warn, view=view, ephemeral=True)
        view.interaction = interaction

    @app_commands.command(name="ad-status", description="See how often your ad was posted and where")
    @app_commands.guild_only()
    async def ad_status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        ad = await current_ad(guild.id)
        cfg = await db.get_guild(guild.id)
        stats = await db.ad_stats(guild.id)
        received = await db.received_count(guild.id)
        recent = await db.recent_deliveries(guild.id, 5)
        channel_id = cfg["channel_id"] if cfg else None

        if stats["total"]:
            description = (
                f"Your ad was posted **{stats['total']:,}** time{'s' if stats['total'] != 1 else ''} "
                f"in **{stats['channels']:,}** different channel{'s' if stats['channels'] != 1 else ''}."
            )
        elif ad:
            description = "Your ad is live but hasn't been posted anywhere yet. Hang tight!"
        else:
            description = "You haven't set an ad yet. Use `/set-ad` to get started."

        embed = embeds.base(embeds.BRAND, "📊 Ad Status", description)
        embed.set_author(name=guild.name, icon_url=guild.icon.with_size(128).url if guild.icon else None)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.with_size(256).url)

        embed.add_field(name="📨 Total Posts", value=f"`{stats['total']:,}`", inline=True)
        embed.add_field(name="📡 Unique Channels", value=f"`{stats['channels']:,}`", inline=True)
        embed.add_field(name="🌍 Unique Servers", value=f"`{stats['servers']:,}`", inline=True)
        embed.add_field(name="📥 Ads Received", value=f"`{received:,}`", inline=True)
        embed.add_field(name="🕒 Last Posted", value=rel(stats["last"]) if stats["last"] else "Never", inline=True)

        interval = config.AD_INTERVAL_MINUTES * 60
        if ad and channel_id:
            last = cfg["last_received_at"]
            next_at = max(last + interval, time.time()) if last else time.time()
            embed.add_field(name="⏭️ Next Incoming Ad", value=rel(next_at), inline=True)
        else:
            embed.add_field(name="⏭️ Next Incoming Ad", value="—", inline=True)

        premium = await db.get_premium(guild.id)
        if premium:
            until = "never expires" if premium["expires_at"] is None else f"until {rel(premium['expires_at'])}"
            premium_text = f"💎 **Active** — {config.PREMIUM_BOOST}x promotion ({until})"
        else:
            premium_text = f"Not active — `/premium` for {config.PREMIUM_BOOST}x more promotion"
        embed.add_field(name="💎 Premium", value=premium_text, inline=False)

        embed.add_field(
            name="⚙️ Setup",
            value=(
                f"{'✅' if ad else '❌'} Ad {'set' if ad else 'missing — `/set-ad`'}\n"
                f"{'✅' if channel_id else '❌'} Channel "
                f"{f'<#{channel_id}>' if channel_id else 'missing — `/setup`'}\n"
                f"🔁 Rotation: 1 ad / **{format_minutes(config.AD_INTERVAL_MINUTES)}**"
            ),
            inline=False,
        )

        if recent:
            lines = []
            for row in recent:
                live = self.bot.get_guild(row["target_guild_id"])
                name = live.name if live else (row["target_guild_name"] or "Unknown server")
                lines.append(f"• **{truncate(name, 40)}** — {rel(row['posted_at'])}")
            embed.add_field(name="🛰️ Recent Placements", value="\n".join(lines), inline=False)

        if ad:
            embed.add_field(name="📝 Current Ad", value=f">>> {truncate(ad['content'], 300)}", inline=False)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ads(bot))
