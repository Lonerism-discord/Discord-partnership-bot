from __future__ import annotations

import math
import platform
import time

import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import embeds
from utils import format_duration, format_minutes, rel

_process = psutil.Process()


def _quality(ms: float) -> tuple[str, discord.Colour]:
    if ms < 150:
        return "🟢 Excellent", embeds.SUCCESS
    if ms < 300:
        return "🟡 Good", embeds.WARN
    return "🔴 Slow", embeds.ERROR


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.presence.start()

    async def cog_unload(self) -> None:
        self.presence.cancel()

    @tasks.loop(minutes=10)
    async def presence(self) -> None:
        await self.bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name=f"{len(self.bot.guilds):,} servers • /add-bot"
            )
        )

    @presence.before_loop
    async def _before_presence(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="status", description="Show the bot's ping, server count and stats")
    async def status(self, interaction: discord.Interaction) -> None:
        started = time.perf_counter()
        await interaction.response.defer()
        api_ms = (time.perf_counter() - started) * 1000
        ws_ms = self.bot.latency * 1000
        ws_ok = math.isfinite(ws_ms)

        quality, colour = _quality(max(api_ms, ws_ms if ws_ok else 0))
        stats = await db.global_stats()
        users = sum(g.member_count or 0 for g in self.bot.guilds)
        uptime = time.time() - self.bot.started_at
        mem_mb = _process.memory_info().rss / 1024 / 1024

        embed = embeds.base(colour, "📡 Bot Status", f"All systems running — **{quality}**")
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="🏓 Gateway Ping", value=f"`{ws_ms:.0f} ms`" if ws_ok else "`n/a`", inline=True)
        embed.add_field(name="⚡ API Latency", value=f"`{api_ms:.0f} ms`", inline=True)
        embed.add_field(
            name="⏱️ Uptime", value=f"`{format_duration(uptime)}`\nSince {rel(self.bot.started_at)}", inline=True
        )
        embed.add_field(name="🌐 Servers", value=f"`{len(self.bot.guilds):,}`", inline=True)
        embed.add_field(name="👥 Users", value=f"`{users:,}`", inline=True)
        embed.add_field(name="🤝 Active Partners", value=f"`{stats['partners']:,}`", inline=True)
        embed.add_field(name="💎 Premium Servers", value=f"`{stats['premium']:,}`", inline=True)
        embed.add_field(name="📢 Ads Registered", value=f"`{stats['ads']:,}`", inline=True)
        embed.add_field(name="📨 Ads Delivered", value=f"`{stats['deliveries']:,}`", inline=True)
        embed.add_field(
            name="🔁 Ad Rotation", value=f"`1 / {format_minutes(config.AD_INTERVAL_MINUTES)}`", inline=True
        )
        embed.add_field(name="💾 Memory", value=f"`{mem_mb:.1f} MB`", inline=True)
        embed.add_field(
            name="🧩 Stack",
            value=f"`Python {platform.python_version()}`\n`discord.py {discord.__version__}`",
            inline=True,
        )
        embed.add_field(name="🖥️ Platform", value=f"`{platform.system()} {platform.release()}`", inline=True)

        buttons = [("Invite Bot", config.INVITE_URL, "➕")] if config.INVITE_URL else []
        if config.SUPPORT_URL:
            buttons.append(("Support", config.SUPPORT_URL, "💬"))
        await interaction.followup.send(embed=embed, view=embeds.link_view(*buttons))

    @app_commands.command(name="info", description="What this bot is for and how premium works")
    async def info(self, interaction: discord.Interaction) -> None:
        embed = embeds.base(
            embeds.BRAND,
            "About Partnership Bot",
            "This bot helps servers grow by swapping ads. You save your ad once, and the bot posts it in "
            "other servers that use the bot. In return, their ads show up in a channel of your choice. "
            "After setup it runs by itself.",
        )
        embed.add_field(
            name="Getting started",
            value=(
                "1. Run /setup and pick the channel that should receive ads.\n"
                "2. Run /set-ad and paste your ad.\n"
                "3. Run /ad-status whenever you want to see how many servers your ad reached."
            ),
            inline=False,
        )
        embed.add_field(
            name="Premium",
            value=(
                f"Premium servers get their ad shown {config.PREMIUM_BOOST} times more often than normal. "
                "Every so often their ad is also pinned in partner servers with an @everyone ping. "
                "That is limited so nobody gets spammed, and it only works where the bot has permission. "
                "You also get a heads up before your premium runs out. "
                f"To get it, DM {config.owner_mention()} for a code, then use /redeem."
            ),
            inline=False,
        )
        embed.add_field(
            name="More info",
            value="Join the support server if you have questions or want to know more."
            if config.SUPPORT_URL
            else "Ask the bot owner if you have questions.",
            inline=False,
        )
        buttons = [("Add Bot", config.INVITE_URL, None)] if config.INVITE_URL else []
        if config.SUPPORT_URL:
            buttons.insert(0, ("Support Server", config.SUPPORT_URL, None))
        await interaction.response.send_message(embed=embed, view=embeds.link_view(*buttons))

    @app_commands.command(name="add-bot", description="Get a button to add this bot to your server")
    async def add_bot(self, interaction: discord.Interaction) -> None:
        embed = embeds.base(
            embeds.BRAND,
            "➕ Add Partnership Bot",
            "Grow your community with automatic server-to-server partnerships. "
            "Click the button below to add the bot to your server.",
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="1️⃣ Add the bot", value="Hit **Add to Server** below.", inline=True)
        embed.add_field(name="2️⃣ Pick a channel", value="Run `/setup` and choose where ads appear.", inline=True)
        embed.add_field(name="3️⃣ Save your ad", value="Run `/set-ad` and paste your ad.", inline=True)
        embed.add_field(
            name="✨ What you get",
            value=(
                "• Your ad posted in other partner servers\n"
                "• Partner ads delivered to your channel\n"
                "• Live stats with `/ad-status`"
            ),
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed,
            view=embeds.link_view(("Add to Server", config.INVITE_URL or "https://discord.com", "➕")),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
