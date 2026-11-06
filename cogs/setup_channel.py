from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
from utils import format_minutes

REQUIRED_PERMS = [
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
]


class SetupChannel(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Choose the channel where partner ads will be posted")
    @app_commands.describe(channel="Channel that will receive partner ads from other servers")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        perms = channel.permissions_for(guild.me)
        missing = [label for attr, label in REQUIRED_PERMS if not getattr(perms, attr)]
        if missing:
            await interaction.followup.send(
                embed=embeds.error(
                    "Missing Permissions",
                    f"I can't post in {channel.mention}. Please give me:\n"
                    + "\n".join(f"• **{m}**" for m in missing),
                ),
                ephemeral=True,
            )
            return

        interval = format_minutes(config.AD_INTERVAL_MINUTES)
        welcome = embeds.base(
            embeds.BRAND,
            "🤝 Partnership Channel Connected",
            f"Partner ads from other servers will appear here — **1 ad every {interval}**.",
        )
        welcome.add_field(
            name="📢 Want your server shown too?",
            value="Use `/set-ad`, paste your ad and save. Servers need **both** a channel and an ad to take part.",
            inline=False,
        )
        welcome.add_field(
            name="🧭 Commands",
            value="`/set-ad` · `/remove-ad` · `/ad-status` · `/status` · `/add-bot`",
            inline=False,
        )
        welcome.set_footer(text=f"{embeds.FOOTER}  •  Set up by {interaction.user.display_name}")
        if guild.icon:
            welcome.set_thumbnail(url=guild.icon.with_size(256).url)

        try:
            await channel.send(embed=welcome)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=embeds.error("Couldn't Post In That Channel", f"Discord said: `{exc.text or exc.status}`"),
                ephemeral=True,
            )
            return

        await db.set_channel(guild.id, channel.id, interaction.user.id)
        ad = await db.get_ad(guild.id)

        done = embeds.success("Setup Complete", f"Partner ads will now be posted in {channel.mention}.")
        done.add_field(name="📍 Ad Channel", value=channel.mention, inline=True)
        done.add_field(name="⏱️ Frequency", value=f"1 ad / **{interval}**", inline=True)
        done.add_field(name="📝 Your Ad", value="✅ Active" if ad else "❌ Not set yet", inline=True)
        done.add_field(
            name="🚀 Next Steps",
            value=(
                "You're all set! Partner ads will start flowing in.\nUse `/ad-status` to track your reach."
                if ad
                else "**1.** Use `/set-ad` and paste your ad\n**2.** Use `/ad-status` to track your reach\n\n"
                "> You need **both** a channel and an ad to take part in the exchange."
            ),
            inline=False,
        )
        if guild.icon:
            done.set_thumbnail(url=guild.icon.with_size(256).url)
        await interaction.followup.send(embed=done, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupChannel(bot))
