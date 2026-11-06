from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import discord

# What the bot needs day to day.
BASIC_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, read_message_history=True
)

# What /fix needs on top (rebuilding deleted channels / roles from the audit log).
RESTORE_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    view_audit_log=True,
    manage_channels=True,
    manage_roles=True,
    kick_members=True,  # only for the optional "Restore + kick" in /fix
)


def oauth_link(application_id: int, permissions: discord.Permissions) -> str:
    """Bot invite link for the given permissions (slash commands work with the plain `bot` scope)."""
    return discord.utils.oauth_url(application_id, permissions=permissions, scopes=("bot",))


def link_permissions(url: str | None) -> discord.Permissions | None:
    """The permissions an invite link asks for (from its `permissions=` parameter), or None if unknown."""
    if not url:
        return None
    try:
        value = parse_qs(urlparse(url).query).get("permissions", [None])[0]
        return discord.Permissions(int(value)) if value is not None else None
    except (ValueError, TypeError):
        return None


INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+",
    re.IGNORECASE,
)


def find_invite(text: str) -> str | None:
    """First Discord invite link in text, normalised to https://, or None."""
    match = INVITE_RE.search(text)
    if not match:
        return None
    link = match.group(0)
    return link if re.match(r"https?://", link, re.IGNORECASE) else f"https://{link}"


def rel(ts: int | float) -> str:
    """Discord relative timestamp ("5 minutes ago") from unix seconds."""
    return f"<t:{int(ts)}:R>"


def stamp(ts: int | float) -> str:
    return f"<t:{int(ts)}:f>"


def format_duration(seconds: float) -> str:
    s = int(seconds)
    units = [(s // 86400, "d"), ((s % 86400) // 3600, "h"), ((s % 3600) // 60, "m"), (s % 60, "s")]
    start = next((i for i, (v, _) in enumerate(units) if v > 0), None)
    if start is None:
        return "0s"
    return " ".join(f"{v}{u}" for v, u in units[start : start + 3])


def format_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
