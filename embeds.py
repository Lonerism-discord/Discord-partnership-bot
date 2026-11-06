from __future__ import annotations

import discord

import config
from utils import find_invite, truncate

BRAND = discord.Colour(0x7C5CFF)
SUCCESS = discord.Colour(0x57F287)
ERROR = discord.Colour(0xED4245)
WARN = discord.Colour(0xFEE75C)
INFO = discord.Colour(0x5865F2)

FOOTER = "Partnership Bot"


def base(colour: discord.Colour, title: str | None = None, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        colour=colour, title=title, description=description, timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=FOOTER)
    return embed


def success(title: str, description: str | None = None) -> discord.Embed:
    return base(SUCCESS, f"✅ {title}", description)


def error(title: str, description: str | None = None) -> discord.Embed:
    return base(ERROR, f"❌ {title}", description)


def warning(title: str, description: str | None = None) -> discord.Embed:
    return base(WARN, f"⚠️ {title}", description)


def info(title: str, description: str | None = None) -> discord.Embed:
    return base(INFO, f"ℹ️ {title}", description)


# Partner ads are posted as plain text (no embed):
#
#   Partnership
#   ---------------------------------------
#   {ad}
#   ---------------------------------------
#   -# want auto-partnerships like this? join us with the button below:
DIVIDER = "---------------------------------------"
PROMO = "-# want auto-partnerships like this? join us with the button below:"
MESSAGE_LIMIT = 2000  # Discord's max length for a bot message
_HEAD = f"Partnership\n{DIVIDER}\n"
_TAIL = f"\n{DIVIDER}\n{PROMO}"
_FENCE_CLOSE = "\n```"
MAX_AD_LENGTH = MESSAGE_LIMIT - len(_HEAD) - len(_TAIL) - len(_FENCE_CLOSE)


PING_LINE = "@everyone\n"  # premium "featured" posts only


def ad_message(content: str, ping: bool = False) -> str:
    """The plain-text message an ad is posted as. Ad text is used as pasted (cut only if it can't fit)."""
    prefix = PING_LINE if ping else ""
    body = truncate(content, MAX_AD_LENGTH - len(prefix))
    if body.count("```") % 2:  # an unclosed code block would swallow the divider + promo line
        body += _FENCE_CLOSE
    return f"{prefix}{_HEAD}{body}{_TAIL}"


def extract_ad(message_content: str) -> str:
    """Pull the ad text back out of a posted partner message."""
    parts = message_content.split(DIVIDER)
    if len(parts) < 3:
        return message_content.strip()
    return DIVIDER.join(parts[1:-1]).strip()


def link_view(*buttons: tuple[str, str, str | None]) -> discord.ui.View:
    """View holding link buttons: (label, url, emoji)."""
    view = discord.ui.View()
    for label, url, emoji in buttons:
        view.add_item(discord.ui.Button(label=label, url=url, emoji=emoji, style=discord.ButtonStyle.link))
    return view


def ad_view(content: str, report_for: int | None = None) -> discord.ui.View:
    """
    Add Bot + Support Server + Join Partnered Server (invite is found in the ad text itself).
    With `report_for` (the advertiser's guild id) a Report button is added too — only for real posts,
    not for the /set-ad preview.
    """
    view = discord.ui.View(timeout=None) if report_for is not None else discord.ui.View()
    links = [("Add Bot", config.INVITE_URL)] if config.INVITE_URL else []
    if config.SUPPORT_URL:
        links.append(("Support Server", config.SUPPORT_URL))
    invite = find_invite(content)
    if invite:
        links.append(("Join Partnered Server", invite))
    for label, url in links:
        view.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link))
    if report_for is not None:
        from cogs.reports import ReportButton  # lazy: cogs import this module

        view.add_item(ReportButton(report_for))
    return view
