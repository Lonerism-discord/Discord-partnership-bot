from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import aiosqlite

import config

_conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id         INTEGER PRIMARY KEY,
    channel_id       INTEGER,
    setup_by         INTEGER,
    last_received_at INTEGER,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ads (
    guild_id   INTEGER PRIMARY KEY,
    content    TEXT NOT NULL,
    updated_by INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    approved   INTEGER NOT NULL DEFAULT 0   -- 1 = owner manually approved this text after a review
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    guild_name  TEXT,
    user_id     INTEGER NOT NULL,
    user_name   TEXT,
    content     TEXT NOT NULL,
    flags       TEXT NOT NULL,          -- JSON list of {category, severity, match}
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | declined
    kind        TEXT NOT NULL DEFAULT 'filter',    -- filter (held before posting) | report (live ad reported)
    created_at  INTEGER NOT NULL,
    decided_by  INTEGER,
    decided_at  INTEGER
);

CREATE TABLE IF NOT EXISTS banned_users (
    user_id   INTEGER PRIMARY KEY,
    reason    TEXT,
    banned_by INTEGER,
    banned_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_guild_id       INTEGER NOT NULL,
    target_guild_id   INTEGER NOT NULL,
    target_guild_name TEXT,
    target_channel_id INTEGER NOT NULL,
    message_id        INTEGER,
    posted_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS codes (
    code              TEXT PRIMARY KEY,
    plan              TEXT NOT NULL,
    created_by        INTEGER,
    created_at        INTEGER NOT NULL,
    redeemed_by       INTEGER,
    redeemed_guild_id INTEGER,
    redeemed_at       INTEGER
);

CREATE TABLE IF NOT EXISTS premium (
    guild_id     INTEGER PRIMARY KEY,
    plan         TEXT NOT NULL,
    expires_at   INTEGER,            -- NULL = lifetime
    activated_by INTEGER,
    updated_at   INTEGER NOT NULL,
    notified_for   INTEGER,          -- expires_at the reminder state below belongs to
    notified_stage INTEGER           -- smallest reminder threshold (seconds) already sent
);

CREATE TABLE IF NOT EXISTS banned_guilds (
    guild_id  INTEGER PRIMARY KEY,
    name      TEXT,
    reason    TEXT,
    banned_by INTEGER,
    banned_at INTEGER NOT NULL,
    setup_by  INTEGER               -- who ran /setup there, so we can tell them if the bot is re-added
);

-- Work queue: the helper bot (owner commands) asks the main bot to do things (DMs, leaving servers, ...).
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,                    -- JSON
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    result      TEXT,                             -- JSON
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Snapshot of the servers the main bot is in, so the helper bot can list / look them up.
CREATE TABLE IF NOT EXISTS bot_guilds (
    guild_id     INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    owner_id     INTEGER,
    member_count INTEGER,
    icon_url     TEXT,
    created_at   INTEGER,
    joined_at    INTEGER
);

-- After /fix: DM the admin once a day until the bot's extra (powerful) permissions are removed.
CREATE TABLE IF NOT EXISTS perm_reminders (
    guild_id     INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,           -- the admin who ran /fix (gets the DMs)
    started_at   INTEGER NOT NULL,
    restored_at  INTEGER,                    -- set when the restore finished
    last_sent_at INTEGER,
    sent_count   INTEGER NOT NULL DEFAULT 0
);

-- Premium "featured" posts (@everyone + pin): last time per channel and the message we pinned.
CREATE TABLE IF NOT EXISTS featured (
    channel_id        INTEGER PRIMARY KEY,
    last_featured_at  INTEGER NOT NULL,
    pinned_message_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_deliveries_ad     ON deliveries (ad_guild_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_target ON deliveries (target_guild_id, ad_guild_id);
"""


PLAN_SECONDS: dict[str, int | None] = {
    "weekly": 7 * 86400,
    "monthly": 30 * 86400,
    "lifetime": None,
}


def now() -> int:
    return int(time.time())


async def init() -> None:
    global _conn
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(config.DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode = WAL")
    await _conn.executescript(SCHEMA)
    # Migrations: add columns that older databases don't have yet.
    for table, column, ddl in (
        ("ads", "approved", "INTEGER NOT NULL DEFAULT 0"),
        ("reviews", "kind", "TEXT NOT NULL DEFAULT 'filter'"),
        ("premium", "notified_for", "INTEGER"),
        ("premium", "notified_stage", "INTEGER"),
        ("banned_guilds", "setup_by", "INTEGER"),
    ):
        columns = {row["name"] for row in await _all(f"PRAGMA table_info({table})")}
        if column not in columns:
            await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await _conn.commit()


async def close() -> None:
    if _conn is not None:
        await _conn.close()


async def _one(sql: str, params: tuple = ()) -> aiosqlite.Row | None:
    async with _conn.execute(sql, params) as cur:
        return await cur.fetchone()


async def _all(sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
    async with _conn.execute(sql, params) as cur:
        return list(await cur.fetchall())


async def _run(sql: str, params: tuple = ()) -> int:
    async with _conn.execute(sql, params) as cur:
        changed = cur.rowcount
    await _conn.commit()
    return changed


# --- guild config -----------------------------------------------------------

async def get_guild(guild_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM guilds WHERE guild_id = ?", (guild_id,))


async def set_channel(guild_id: int, channel_id: int, user_id: int) -> None:
    await _run(
        """
        INSERT INTO guilds (guild_id, channel_id, setup_by, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, setup_by = excluded.setup_by
        """,
        (guild_id, channel_id, user_id, now()),
    )


async def clear_channel(guild_id: int) -> None:
    await _run("UPDATE guilds SET channel_id = NULL WHERE guild_id = ?", (guild_id,))


async def mark_received(guild_id: int, ts: int | None = None) -> None:
    await _run("UPDATE guilds SET last_received_at = ? WHERE guild_id = ?", (ts or now(), guild_id))


async def delete_guild_data(guild_id: int) -> None:
    await _run("DELETE FROM guilds WHERE guild_id = ?", (guild_id,))
    await _run("DELETE FROM ads WHERE guild_id = ?", (guild_id,))


# --- ads --------------------------------------------------------------------

async def get_ad(guild_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM ads WHERE guild_id = ?", (guild_id,))


async def set_ad(guild_id: int, content: str, user_id: int, approved: bool = False) -> None:
    ts = now()
    await _run(
        """
        INSERT INTO ads (guild_id, content, updated_by, created_at, updated_at, approved)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            content = excluded.content, updated_by = excluded.updated_by,
            updated_at = excluded.updated_at, approved = excluded.approved
        """,
        (guild_id, content, user_id, ts, ts, int(approved)),
    )


async def remove_ad(guild_id: int) -> bool:
    return await _run("DELETE FROM ads WHERE guild_id = ?", (guild_id,)) > 0


# --- deliveries / stats -----------------------------------------------------

async def record_delivery(
    ad_guild_id: int, target_guild_id: int, target_guild_name: str, target_channel_id: int, message_id: int
) -> None:
    await _run(
        """
        INSERT INTO deliveries
            (ad_guild_id, target_guild_id, target_guild_name, target_channel_id, message_id, posted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ad_guild_id, target_guild_id, target_guild_name, target_channel_id, message_id, now()),
    )


async def deliveries_for_ad(ad_guild_id: int) -> list[aiosqlite.Row]:
    """Every posted copy of a server's ad (channel + message id), so it can be taken down."""
    return await _all(
        "SELECT target_channel_id, message_id FROM deliveries WHERE ad_guild_id = ? AND message_id IS NOT NULL",
        (ad_guild_id,),
    )


async def ad_stats(guild_id: int) -> dict:
    row = await _one(
        """
        SELECT COUNT(*)                          AS total,
               COUNT(DISTINCT target_channel_id) AS channels,
               COUNT(DISTINCT target_guild_id)   AS servers,
               MAX(posted_at)                    AS last
        FROM deliveries WHERE ad_guild_id = ?
        """,
        (guild_id,),
    )
    return dict(row)


async def recent_deliveries(guild_id: int, limit: int = 5) -> list[aiosqlite.Row]:
    return await _all(
        """
        SELECT target_guild_id, target_guild_name, target_channel_id, posted_at
        FROM deliveries WHERE ad_guild_id = ? ORDER BY posted_at DESC, id DESC LIMIT ?
        """,
        (guild_id, limit),
    )


async def received_count(guild_id: int) -> int:
    row = await _one("SELECT COUNT(*) AS c FROM deliveries WHERE target_guild_id = ?", (guild_id,))
    return row["c"]


# --- scheduler queries ------------------------------------------------------

async def get_due_guilds(cutoff: int) -> list[aiosqlite.Row]:
    """Guilds with a channel AND an ad that are due for their next partner ad."""
    return await _all(
        """
        SELECT g.* FROM guilds g
        JOIN ads a ON a.guild_id = g.guild_id
        WHERE g.channel_id IS NOT NULL AND (g.last_received_at IS NULL OR g.last_received_at <= ?)
        """,
        (cutoff,),
    )


async def get_setup_users() -> list[int]:
    """Distinct users who ran /setup in a server the bot is still configured in."""
    rows = await _all(
        """
        SELECT DISTINCT setup_by FROM guilds
        WHERE setup_by IS NOT NULL AND setup_by NOT IN (SELECT user_id FROM banned_users)
        ORDER BY setup_by
        """
    )
    return [r["setup_by"] for r in rows]


async def get_participants() -> list[aiosqlite.Row]:
    """Every guild that has both a channel and an ad, due or not."""
    return await _all(
        """
        SELECT g.* FROM guilds g
        JOIN ads a ON a.guild_id = g.guild_id
        WHERE g.channel_id IS NOT NULL
        """
    )


async def get_candidate_ads(target_guild_id: int) -> list[aiosqlite.Row]:
    """Ads from other participating guilds, least recently shown to this target first."""
    return await _all(
        """
        SELECT a.guild_id, a.content, a.approved, a.updated_by, MAX(d.posted_at) AS last_seen,
               CASE WHEN p.guild_id IS NOT NULL AND (p.expires_at IS NULL OR p.expires_at > ?)
                    THEN 1 ELSE 0 END AS premium
        FROM ads a
        JOIN guilds g ON g.guild_id = a.guild_id AND g.channel_id IS NOT NULL
        LEFT JOIN deliveries d ON d.ad_guild_id = a.guild_id AND d.target_guild_id = ?
        LEFT JOIN premium p ON p.guild_id = a.guild_id
        WHERE a.guild_id != ?
        GROUP BY a.guild_id
        ORDER BY last_seen ASC, RANDOM()
        """,
        (now(), target_guild_id, target_guild_id),
    )


async def global_stats() -> dict:
    partners = await _one(
        "SELECT COUNT(*) AS c FROM guilds g JOIN ads a ON a.guild_id = g.guild_id WHERE g.channel_id IS NOT NULL"
    )
    ads = await _one("SELECT COUNT(*) AS c FROM ads")
    deliveries = await _one("SELECT COUNT(*) AS c FROM deliveries")
    premium = await _one(
        "SELECT COUNT(*) AS c FROM premium WHERE expires_at IS NULL OR expires_at > ?", (now(),)
    )
    return {
        "partners": partners["c"],
        "ads": ads["c"],
        "deliveries": deliveries["c"],
        "premium": premium["c"],
    }


# --- job queue (helper bot -> main bot) -------------------------------------

async def create_job(kind: str, payload: dict) -> int:
    async with _conn.execute(
        "INSERT INTO jobs (kind, payload, created_at) VALUES (?, ?, ?)", (kind, json.dumps(payload), now())
    ) as cur:
        job_id = cur.lastrowid
    await _conn.commit()
    return job_id


async def claim_next_job() -> aiosqlite.Row | None:
    """Oldest pending job, atomically flipped to 'running' (None if there is none / another worker won)."""
    row = await _one("SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1")
    if row is None:
        return None
    won = await _run("UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'pending'", (row["id"],))
    return row if won else None


async def finish_job(job_id: int, status: str, result: dict) -> None:
    await _run(
        "UPDATE jobs SET status = ?, result = ?, finished_at = ? WHERE id = ?",
        (status, json.dumps(result), now(), job_id),
    )


async def get_job(job_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM jobs WHERE id = ?", (job_id,))


async def cancel_job_if_pending(job_id: int) -> bool:
    return await _run("UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'pending'", (now(), job_id)) > 0


async def reset_stale_jobs() -> None:
    """On main-bot start: jobs left 'running' by a crash can't finish any more; old finished jobs are pruned."""
    await _run(
        "UPDATE jobs SET status = 'failed', result = ?, finished_at = ? WHERE status = 'running'",
        (json.dumps({"error": "the main bot restarted while running this"}), now()),
    )
    await _run("DELETE FROM jobs WHERE status != 'pending' AND created_at < ?", (now() - 86400,))


async def set_meta(key: str, value: str) -> None:
    await _run(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def get_meta(key: str) -> str | None:
    row = await _one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else None


async def main_alive(max_age: int = 15) -> bool:
    """True if the main bot's job runner checked in recently."""
    beat = await get_meta("main_heartbeat")
    return beat is not None and now() - int(beat) <= max_age


# --- snapshot of the main bot's servers -------------------------------------

async def sync_bot_guilds(rows: list[tuple]) -> None:
    """Replace the snapshot: (guild_id, name, owner_id, member_count, icon_url, created_at, joined_at)."""
    await _conn.execute("DELETE FROM bot_guilds")
    await _conn.executemany("INSERT INTO bot_guilds VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    await _conn.commit()


async def upsert_bot_guild(row: tuple) -> None:
    await _run(
        """
        INSERT INTO bot_guilds VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            name = excluded.name, owner_id = excluded.owner_id, member_count = excluded.member_count,
            icon_url = excluded.icon_url, created_at = excluded.created_at, joined_at = excluded.joined_at
        """,
        row,
    )


async def remove_bot_guild(guild_id: int) -> None:
    await _run("DELETE FROM bot_guilds WHERE guild_id = ?", (guild_id,))


async def get_bot_guild(guild_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM bot_guilds WHERE guild_id = ?", (guild_id,))


async def search_bot_guilds(query: str, limit: int = 25) -> list[aiosqlite.Row]:
    like = f"%{query.strip().lower()}%"
    return await _all(
        "SELECT * FROM bot_guilds WHERE lower(name) LIKE ? OR CAST(guild_id AS TEXT) LIKE ? ORDER BY name LIMIT ?",
        (like, like, limit),
    )


# --- moderation: reviews + bans ---------------------------------------------

async def create_review(
    guild_id: int,
    guild_name: str,
    user_id: int,
    user_name: str,
    content: str,
    flags_json: str,
    kind: str = "filter",
) -> tuple[int, bool]:
    """Queue content for owner review. Returns (review_id, created); identical pending items are reused."""
    existing = await _one(
        "SELECT id FROM reviews WHERE guild_id = ? AND content = ? AND kind = ? AND status = 'pending'",
        (guild_id, content, kind),
    )
    if existing:
        return existing["id"], False
    async with _conn.execute(
        """
        INSERT INTO reviews (guild_id, guild_name, user_id, user_name, content, flags, kind, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, guild_name, user_id, user_name, content, flags_json, kind, now()),
    ) as cur:
        review_id = cur.lastrowid
    await _conn.commit()
    return review_id, True


async def get_review(review_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM reviews WHERE id = ?", (review_id,))


async def pending_reviews() -> list[aiosqlite.Row]:
    return await _all("SELECT * FROM reviews WHERE status = 'pending' ORDER BY id")


async def pending_review_for_guild(guild_id: int) -> aiosqlite.Row | None:
    """The ad this server submitted that is waiting on review (reports don't count — those ads are live)."""
    return await _one(
        "SELECT * FROM reviews WHERE guild_id = ? AND kind = 'filter' AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (guild_id,),
    )


async def decide_review(review_id: int, status: str, decided_by: int) -> bool:
    """pending -> accepted/declined. False if it was already decided."""
    return (
        await _run(
            "UPDATE reviews SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (status, decided_by, now(), review_id),
        )
        > 0
    )


async def ban_user(user_id: int, reason: str, banned_by: int) -> None:
    await _run(
        """
        INSERT INTO banned_users (user_id, reason, banned_by, banned_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, banned_by = excluded.banned_by
        """,
        (user_id, reason, banned_by, now()),
    )


async def unban_user(user_id: int) -> bool:
    return await _run("DELETE FROM banned_users WHERE user_id = ?", (user_id,)) > 0


async def is_banned(user_id: int) -> bool:
    return await _one("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)) is not None


async def ban_guild(
    guild_id: int, name: str, reason: str, banned_by: int, setup_by: int | None = None
) -> None:
    await _run(
        """
        INSERT INTO banned_guilds (guild_id, name, reason, banned_by, banned_at, setup_by)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            name = excluded.name, reason = excluded.reason, banned_by = excluded.banned_by,
            setup_by = COALESCE(excluded.setup_by, banned_guilds.setup_by)
        """,
        (guild_id, name, reason, banned_by, now(), setup_by),
    )


async def unban_guild(guild_id: int) -> bool:
    return await _run("DELETE FROM banned_guilds WHERE guild_id = ?", (guild_id,)) > 0


async def get_banned_guild(guild_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM banned_guilds WHERE guild_id = ?", (guild_id,))


async def banned_guild_ids() -> list[int]:
    return [r["guild_id"] for r in await _all("SELECT guild_id FROM banned_guilds")]


# --- /fix: reminders to remove the extra permissions ---------------------------

async def track_perm_reminder(guild_id: int, user_id: int) -> None:
    """Start (or restart) the daily-reminder cycle for a server after /fix."""
    await _run(
        """
        INSERT INTO perm_reminders (guild_id, user_id, started_at) VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            user_id = excluded.user_id, started_at = excluded.started_at,
            restored_at = NULL, last_sent_at = NULL, sent_count = 0
        """,
        (guild_id, user_id, now()),
    )


async def mark_restored(guild_id: int) -> None:
    await _run("UPDATE perm_reminders SET restored_at = ? WHERE guild_id = ?", (now(), guild_id))


async def list_perm_reminders() -> list[aiosqlite.Row]:
    return await _all("SELECT * FROM perm_reminders")


async def mark_perm_reminder_sent(guild_id: int) -> None:
    await _run(
        "UPDATE perm_reminders SET last_sent_at = ?, sent_count = sent_count + 1 WHERE guild_id = ?",
        (now(), guild_id),
    )


async def delete_perm_reminder(guild_id: int) -> None:
    await _run("DELETE FROM perm_reminders WHERE guild_id = ?", (guild_id,))


# --- premium featured posts -------------------------------------------------

async def get_featured(channel_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM featured WHERE channel_id = ?", (channel_id,))


async def set_featured(channel_id: int, pinned_message_id: int | None) -> None:
    await _run(
        """
        INSERT INTO featured (channel_id, last_featured_at, pinned_message_id) VALUES (?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            last_featured_at = excluded.last_featured_at,
            pinned_message_id = COALESCE(excluded.pinned_message_id, featured.pinned_message_id)
        """,
        (channel_id, now(), pinned_message_id),
    )


async def set_pinned_message(channel_id: int, message_id: int | None) -> None:
    await _run("UPDATE featured SET pinned_message_id = ? WHERE channel_id = ?", (message_id, channel_id))


# --- premium expiry reminders -----------------------------------------------

async def premium_expiring(within_seconds: int) -> list[aiosqlite.Row]:
    """Timed premiums that end within `within_seconds` (lifetime and already-expired rows are skipped)."""
    ts = now()
    return await _all(
        "SELECT * FROM premium WHERE expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?",
        (ts, ts + within_seconds),
    )


async def mark_reminded(guild_id: int, expires_at: int, stage: int) -> None:
    await _run(
        "UPDATE premium SET notified_for = ?, notified_stage = ? WHERE guild_id = ?",
        (expires_at, stage, guild_id),
    )


# --- premium ----------------------------------------------------------------

async def create_codes(plan: str, amount: int, user_id: int) -> list[str]:
    """Generate `amount` single-use UUID codes for a plan (weekly / monthly / lifetime)."""
    if plan not in PLAN_SECONDS:
        raise ValueError(f"unknown plan {plan!r}")
    ts = now()
    codes = [str(uuid.uuid4()) for _ in range(amount)]
    await _conn.executemany(
        "INSERT INTO codes (code, plan, created_by, created_at) VALUES (?, ?, ?, ?)",
        [(code, plan, user_id, ts) for code in codes],
    )
    await _conn.commit()
    return codes


async def get_premium(guild_id: int) -> aiosqlite.Row | None:
    """The guild's premium row if it is currently active, else None."""
    return await _one(
        "SELECT * FROM premium WHERE guild_id = ? AND (expires_at IS NULL OR expires_at > ?)",
        (guild_id, now()),
    )


async def redeem_code(code: str, guild_id: int, user_id: int) -> tuple[str, dict | None]:
    """
    Redeem a code for a guild. Returns (status, info):
      "invalid"  – no such code
      "used"     – already redeemed
      "lifetime" – guild already has lifetime premium (code NOT consumed)
      "ok"       – info = {plan, expires_at (None = lifetime), extended}
    """
    row = await _one("SELECT * FROM codes WHERE code = ?", (code,))
    if row is None:
        return "invalid", None
    if row["redeemed_at"] is not None:
        return "used", None

    current = await get_premium(guild_id)
    if current is not None and current["expires_at"] is None:
        return "lifetime", None

    ts = now()
    # Atomic claim: only one redeemer can flip redeemed_at from NULL.
    claimed = await _run(
        "UPDATE codes SET redeemed_by = ?, redeemed_guild_id = ?, redeemed_at = ? "
        "WHERE code = ? AND redeemed_at IS NULL",
        (user_id, guild_id, ts, code),
    )
    if claimed == 0:
        return "used", None

    plan = row["plan"]
    seconds = PLAN_SECONDS[plan]
    if seconds is None:
        expires_at = None
    else:
        start = max(ts, current["expires_at"]) if current is not None else ts
        expires_at = start + seconds

    await _run(
        """
        INSERT INTO premium (guild_id, plan, expires_at, activated_by, updated_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            plan = excluded.plan, expires_at = excluded.expires_at,
            activated_by = excluded.activated_by, updated_at = excluded.updated_at
        """,
        (guild_id, plan, expires_at, user_id, ts),
    )
    return "ok", {"plan": plan, "expires_at": expires_at, "extended": current is not None}
