from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from discord.ext import commands, tasks

import config

log = logging.getLogger(__name__)

BACKUP_DIR = config.BASE_DIR / "backups"


def make_backup() -> Path:
    """Consistent snapshot of the live database (safe while the bots are running), then prune old ones."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"bot-{time.strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(config.DB_PATH)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    for old in sorted(BACKUP_DIR.glob("bot-*.db"))[: -config.BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dest


def latest_backup() -> Path | None:
    files = sorted(BACKUP_DIR.glob("bot-*.db"))
    return files[-1] if files else None


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.auto_backup.start()

    async def cog_unload(self) -> None:
        self.auto_backup.cancel()

    @tasks.loop(hours=1)
    async def auto_backup(self) -> None:
        try:
            latest = latest_backup()
            if latest and time.time() - latest.stat().st_mtime < config.BACKUP_EVERY_HOURS * 3600:
                return
            path = await asyncio.to_thread(make_backup)
            log.info("Database backup written: %s", path.name)
        except Exception:
            log.exception("Automatic backup failed")

    @auto_backup.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backup(bot))
