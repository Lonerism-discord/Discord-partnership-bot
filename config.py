import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value.isdigit() else None


def _url(name: str) -> str | None:
    """Link buttons need a full http(s) URL; anything else would make every post fail, so ignore it."""
    value = os.getenv(name, "").strip()
    return value if value.lower().startswith(("http://", "https://")) else None


def _id_set(name: str) -> set[int]:
    return {int(part) for part in os.getenv(name, "").replace(" ", "").split(",") if part.isdigit()}


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEV_GUILD_ID = _int("DEV_GUILD_ID")
SUPPORT_URL = _url("SUPPORT_URL")

# Separate bot (helper_bot/) that hosts every owner command.
HELPER_TOKEN = os.getenv("HELPER_DISCORD_TOKEN", "").strip()

# Invite links. Leave them empty and main.py builds them from the running bot's own application ID:
#   INVITE_URL          normal invite (View Channel, Send Messages, Embed Links, Read Message History)
#   RESTORE_INVITE_URL  invite with the extra permissions /fix needs; shown when /fix lacks permissions
INVITE_URL = _url("INVITE_URL")
RESTORE_INVITE_URL = _url("RESTORE_INVITE_URL")

# Users allowed to use the owner commands on the helper bot. Comma-separated user IDs; empty = nobody.
OWNER_IDS = _id_set("OWNER_IDS")

# /fix: whose deletions to undo when no user ID is typed. Empty = the ID must always be typed.
FIX_DEFAULT_USER_ID = _int("FIX_DEFAULT_USER_ID")

# Premium servers' ads are shown this many times more often.
PREMIUM_BOOST = 3

# Premium "featured" posts (@everyone ping + pin) happen at most once per channel per this many hours,
# so receiving servers don't get pinged on every premium rotation.
FEATURE_COOLDOWN_HOURS = 6

# Database backups (backups/ folder): how often, and how many to keep.
BACKUP_EVERY_HOURS = 24
BACKUP_KEEP = 7

_interval = _int("AD_INTERVAL_MINUTES")
AD_INTERVAL_MINUTES = _interval if _interval and _interval >= 1 else 30

DB_PATH = os.getenv("DB_PATH", "").strip() or str(BASE_DIR / "data" / "bot.db")


def owner_mention() -> str:
    """Who to tell people to DM (e.g. for premium codes)."""
    return f"<@{min(OWNER_IDS)}>" if OWNER_IDS else "the bot owner"
