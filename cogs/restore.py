"""
/fix: rebuild the channels and roles a user deleted, using the server's own audit log.

What it does, step by step:
  1. A server Administrator runs /fix (optionally with a user ID; otherwise FIX_DEFAULT_USER_ID).
  2. The bot reads the audit log entries *made by that user* for "role deleted" and "channel deleted".
  3. It shows a preview and waits for the admin to press "Restore now".
  4. It recreates the roles and channels (names, colours, topics, ... whatever the audit log kept).
  5. It DMs the admin what could NOT be recovered (order, permissions) and tells them to remove the
     bot's extra permissions afterwards. If they don't, they get a DM every day until they do.

What it never does: delete or edit anything that already exists, or ban anyone. Besides creating, the one
other thing it can do is KICK the user whose deletions it is repairing, and only when the admin presses the
separate "Restore + kick" / "Kick them" button. It never kicks the server owner, the admin who ran /fix, itself,
or anyone whose highest role is not below both the admin's and the bot's (same rule Discord applies to people).
New roles come back with NO permissions and new channels come back hidden from @everyone, so a
private channel can never become public by accident; the admin re-permissions them.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
import embeds
from utils import link_permissions, truncate

log = logging.getLogger(__name__)

MAX_ITEMS = 150  # per run, so a single /fix can't hammer the API forever
AUDIT_SCAN_LIMIT = 1000  # audit entries examined per action type (Discord keeps ~45 days)
CREATE_DELAY = 0.7  # seconds between creations
PROGRESS_EVERY = 10
REMINDER_EVERY = 24 * 3600  # daily
WAIT_BEFORE_GIVING_UP = 3 * 24 * 3600  # stop waiting for a /fix that never got finished

# Powerful permissions the bot only needs for /fix. The admin is told to remove these afterwards.
RISKY_PERMISSIONS = [
    ("moderate_members", "Moderate Members"),
    ("manage_events", "Manage Events"),
    ("kick_members", "Kick Members"),
    ("manage_channels", "Manage Channels"),
    ("manage_roles", "Manage Roles"),
    ("manage_guild", "Manage Server"),
]
RISKY_TEXT = "Moderate Members, Manage Events, Kick Members, Manage Channels, Manage Roles and Manage Server"

HOW_TO_REMOVE = (
    f"Take these permissions away from my role so nothing can go wrong if I'm ever misused: **{RISKY_TEXT}**.\n"
    "Server Settings > Roles > my role > switch those permissions off."
)

NOT_RECOVERABLE = (
    "Discord does not keep everything, so some things can't be recovered:\n"
    "• **The order of channels and which category they sit in.** Re-organize the channels yourself.\n"
    "• **Permissions.** Re-permission the roles and channels yourself. New roles have **no permissions**. "
    "New channels are **hidden from @everyone** until you set their permissions, so a private channel never "
    "becomes public by accident."
)

_KIND_BY_TYPE = {
    discord.ChannelType.text: "text",
    discord.ChannelType.news: "news",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.category: "category",
}
_KIND_LABEL = {
    "text": "text",
    "news": "announcement",
    "voice": "voice",
    "stage": "stage",
    "forum": "forum",
    "category": "category",
}


# --- reading the audit log -------------------------------------------------------

@dataclass
class RoleItem:
    old_id: int
    name: str
    colour: int
    hoist: bool
    mentionable: bool
    deleted_at: float


@dataclass
class ChannelItem:
    old_id: int
    name: str
    kind: str  # text | news | voice | stage | forum | category
    topic: str | None
    nsfw: bool
    slowmode: int
    bitrate: int | None
    user_limit: int | None
    parent_id: int | None
    deleted_at: float


def kind_of(value) -> str:
    """Our channel kind for a discord.ChannelType (or the raw int the API sends)."""
    if not isinstance(value, discord.ChannelType):
        try:
            value = discord.ChannelType(value)
        except (ValueError, TypeError):
            return "text"
    return _KIND_BY_TYPE.get(value, "text")


def group_of(kind: str) -> str:
    """Kinds that can't share a name: announcement channels count as text channels."""
    return "text" if kind == "news" else kind


def _get(obj, *names, default=None):
    """First attribute of `obj` that exists and isn't None (audit-log diffs only carry what changed)."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _target_id(entry) -> int | None:
    """ID of the deleted object. For a deleted channel / role the entry's target is a bare Object."""
    target = getattr(entry, "target", None)
    value = getattr(target, "id", None)
    return int(value) if value is not None else None


def role_item(entry) -> RoleItem | None:
    before = entry.before
    name = _get(before, "name")
    old_id = _target_id(entry)
    if not name or old_id is None:
        return None
    colour = _get(before, "colour", "color", default=0)
    return RoleItem(
        old_id=old_id,
        name=str(name),
        colour=int(getattr(colour, "value", colour) or 0),
        hoist=bool(_get(before, "hoist", default=False)),
        mentionable=bool(_get(before, "mentionable", default=False)),
        deleted_at=entry.created_at.timestamp(),
    )


def channel_item(entry) -> ChannelItem | None:
    before = entry.before
    name = _get(before, "name")
    old_id = _target_id(entry)
    if not name or old_id is None:
        return None
    parent = _get(before, "category", "parent_id")
    parent_id = getattr(parent, "id", parent)
    try:
        parent_id = int(parent_id) if parent_id is not None else None
    except (TypeError, ValueError):
        parent_id = None
    return ChannelItem(
        old_id=old_id,
        name=str(name),
        kind=kind_of(_get(before, "type")),
        topic=_get(before, "topic"),
        nsfw=bool(_get(before, "nsfw", default=False)),
        slowmode=int(_get(before, "slowmode_delay", "rate_limit_per_user", default=0) or 0),
        bitrate=_get(before, "bitrate"),
        user_limit=_get(before, "user_limit"),
        parent_id=parent_id,
        deleted_at=entry.created_at.timestamp(),
    )


async def collect(guild: discord.Guild, target_id: int) -> tuple[list[RoleItem], list[ChannelItem]]:
    """Everything `target_id` deleted that the audit log still remembers (newest first, one per object)."""
    who = discord.Object(id=target_id)
    roles: dict[int, RoleItem] = {}
    channels: dict[int, ChannelItem] = {}
    async for entry in guild.audit_logs(limit=AUDIT_SCAN_LIMIT, user=who, action=discord.AuditLogAction.role_delete):
        item = role_item(entry)
        if item and item.old_id not in roles:
            roles[item.old_id] = item
    async for entry in guild.audit_logs(limit=AUDIT_SCAN_LIMIT, user=who, action=discord.AuditLogAction.channel_delete):
        item = channel_item(entry)
        if item and item.old_id not in channels:
            channels[item.old_id] = item
    return list(roles.values()), list(channels.values())


# --- planning ---------------------------------------------------------------------

@dataclass
class Plan:
    roles: list[RoleItem] = field(default_factory=list)
    categories: list[ChannelItem] = field(default_factory=list)
    channels: list[ChannelItem] = field(default_factory=list)
    skipped: int = 0  # already exist (or duplicates), so left alone
    truncated: bool = False

    @property
    def total(self) -> int:
        return len(self.roles) + len(self.categories) + len(self.channels)


def build_plan(guild: discord.Guild, roles: list[RoleItem], channels: list[ChannelItem]) -> Plan:
    """What actually needs creating: skip anything that exists again already, and cap the run size."""
    plan = Plan()
    have_roles = {r.name.lower() for r in guild.roles}
    have_channels = {(c.name.lower(), group_of(kind_of(c.type))) for c in guild.channels}

    wanted_roles: list[RoleItem] = []
    for item in roles:
        key = item.name.lower()
        if key in have_roles:
            plan.skipped += 1
            continue
        have_roles.add(key)
        wanted_roles.append(item)

    wanted_channels: list[ChannelItem] = []
    for item in channels:
        key = (item.name.lower(), group_of(item.kind))
        if key in have_channels:
            plan.skipped += 1
            continue
        have_channels.add(key)
        wanted_channels.append(item)

    budget = MAX_ITEMS
    for source, target in (
        (wanted_roles, plan.roles),
        ([c for c in wanted_channels if c.kind == "category"], plan.categories),
        ([c for c in wanted_channels if c.kind != "category"], plan.channels),
    ):
        take = source[:budget]
        target.extend(take)
        budget -= len(take)
        plan.truncated = plan.truncated or len(take) < len(source)
    return plan


@dataclass
class KickInfo:
    member: discord.Member | None = None
    problem: str | None = None  # why kicking is not possible (None = it is possible)

    @property
    def possible(self) -> bool:
        return self.member is not None and self.problem is None


async def check_kick(guild: discord.Guild, invoker, target_id: int) -> KickInfo:
    """
    Can this user be kicked right now? Never the owner, the admin themselves, the bot, or anyone whose highest
    role is not below both the admin's and the bot's. (Looked up over the API: the bot has no member cache.)
    """
    try:
        member = await guild.fetch_member(target_id)
    except discord.NotFound:
        return KickInfo(None, "they are not in this server, so there is nobody to kick")
    except discord.HTTPException:
        return KickInfo(None, "I couldn't look them up")
    me = guild.me
    if member.id == me.id:
        return KickInfo(member, "that is me")
    if member.id == guild.owner_id:
        return KickInfo(member, "they own this server")
    if member.id == invoker.id:
        return KickInfo(member, "that is you")
    if invoker.id != guild.owner_id and member.top_role >= invoker.top_role:
        return KickInfo(member, "their highest role is as high as yours or higher")
    if not me.guild_permissions.kick_members:
        return KickInfo(member, "I am missing the Kick Members permission (the re-add link includes it)")
    if member.top_role >= me.top_role:
        return KickInfo(member, "their highest role is above mine (move my role up)")
    return KickInfo(member)


def permissions_missing(plan: Plan, perms: discord.Permissions) -> list[tuple[str, str]]:
    """(attribute, label) for every permission the planned restore needs but the bot lacks."""
    needed = []
    if plan.roles:
        needed.append(("manage_roles", "Manage Roles"))
    if plan.categories or plan.channels:
        needed.append(("manage_channels", "Manage Channels"))
    return [(attr, label) for attr, label in needed if not getattr(perms, attr)]


# --- doing it ---------------------------------------------------------------------

@dataclass
class Result:
    roles_ok: list[str] = field(default_factory=list)
    roles_failed: list[tuple[str, str]] = field(default_factory=list)
    channels_ok: list[str] = field(default_factory=list)
    channels_failed: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _why(exc: discord.HTTPException) -> str:
    return truncate(exc.text or f"HTTP {exc.status}", 80)


def _hidden_overwrites(guild: discord.Guild) -> dict:
    """New channels start hidden from @everyone; the bot keeps access so it can finish the job."""
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True),
    }


async def create_channel(guild: discord.Guild, item: ChannelItem, category, reason: str):
    base: dict = {"reason": reason, "overwrites": _hidden_overwrites(guild)}
    if item.kind == "category":
        return await guild.create_category(item.name, **base)

    if category is not None:
        base["category"] = category
    textlike = dict(base)
    if item.topic:
        textlike["topic"] = str(item.topic)[:1024]
    textlike["nsfw"] = item.nsfw
    if item.slowmode:
        textlike["slowmode_delay"] = min(item.slowmode, 21600)

    if item.kind == "news":
        try:
            return await guild.create_text_channel(item.name, news=True, **textlike)
        except discord.Forbidden:
            raise
        except discord.HTTPException:
            pass  # not a Community server: fall through to a normal text channel
    elif item.kind == "forum":
        try:
            return await guild.create_forum(item.name, **textlike)
        except discord.Forbidden:
            raise
        except discord.HTTPException:
            pass  # not a Community server: fall through to a normal text channel
    elif item.kind == "voice":
        voice = dict(base)
        if item.bitrate:
            voice["bitrate"] = max(8000, min(int(item.bitrate), int(guild.bitrate_limit)))
        if item.user_limit:
            voice["user_limit"] = min(int(item.user_limit), 99)
        return await guild.create_voice_channel(item.name, **voice)
    elif item.kind == "stage":
        return await guild.create_stage_channel(item.name, **base)

    return await guild.create_text_channel(item.name, **textlike)


async def apply(guild: discord.Guild, plan: Plan, reason: str, progress=None) -> Result:
    """Create everything in the plan: roles, then categories, then channels. Only ever creates."""
    result = Result()
    done = 0
    roles_blocked = channels_blocked = False
    forbidden_streak = 0

    async def step() -> None:
        nonlocal done
        done += 1
        await asyncio.sleep(CREATE_DELAY)
        if progress and done % PROGRESS_EVERY == 0:
            await progress(done, plan.total)

    for item in plan.roles:
        if roles_blocked:
            result.roles_failed.append((item.name, "skipped"))
            continue
        try:
            await guild.create_role(
                name=item.name,
                colour=discord.Colour(item.colour),
                hoist=item.hoist,
                mentionable=item.mentionable,
                permissions=discord.Permissions.none(),  # permissions are not restored on purpose
                reason=reason,
            )
            result.roles_ok.append(item.name)
            forbidden_streak = 0
        except discord.Forbidden:
            result.roles_failed.append((item.name, "missing permission"))
            forbidden_streak += 1
            if forbidden_streak >= 3:
                roles_blocked = True
                result.notes.append("I kept getting 'missing permissions' while creating roles. Check that my role has Manage Roles.")
        except discord.HTTPException as exc:
            result.roles_failed.append((item.name, _why(exc)))
            if exc.code == 30005:  # maximum number of roles
                roles_blocked = True
                result.notes.append("The server reached Discord's maximum number of roles.")
        await step()

    new_categories: dict[int, discord.abc.GuildChannel] = {}
    forbidden_streak = 0
    for item in plan.categories + plan.channels:
        if channels_blocked:
            result.channels_failed.append((item.name, "skipped"))
            continue
        category = None
        if item.kind != "category" and item.parent_id is not None:
            category = new_categories.get(item.parent_id)
            if category is None:
                existing = guild.get_channel(item.parent_id)
                category = existing if isinstance(existing, discord.CategoryChannel) else None
        try:
            created = await create_channel(guild, item, category, reason)
            result.channels_ok.append(item.name)
            if item.kind == "category":
                new_categories[item.old_id] = created
            forbidden_streak = 0
        except discord.Forbidden:
            result.channels_failed.append((item.name, "missing permission"))
            forbidden_streak += 1
            if forbidden_streak >= 3:
                channels_blocked = True
                result.notes.append("I kept getting 'missing permissions' while creating channels. Check that my role has Manage Channels.")
        except discord.HTTPException as exc:
            result.channels_failed.append((item.name, _why(exc)))
            if exc.code == 30013:  # maximum number of channels
                channels_blocked = True
                result.notes.append("The server reached Discord's maximum number of channels.")
        await step()
    return result


# --- messages ---------------------------------------------------------------------

def _lines(items: list[str], limit: int = 15) -> str:
    shown = [f"• {truncate(i, 60)}" for i in items[:limit]]
    if len(items) > limit:
        shown.append(f"…and {len(items) - limit} more")
    return "\n".join(shown) or "None"


def preview_embed(target_id: int, plan: Plan, kick: KickInfo | None = None) -> discord.Embed:
    if plan.total:
        embed = embeds.base(
            embeds.WARN,
            "Restore preview",
            f"These are things <@{target_id}> (`{target_id}`) deleted, according to the audit log. "
            "Choose a button below and I will recreate them.",
        )
    else:
        embed = embeds.base(
            embeds.WARN,
            "Nothing left to rebuild",
            f"Everything <@{target_id}> (`{target_id}`) deleted already exists again.",
        )
    if plan.roles:
        embed.add_field(name=f"Roles ({len(plan.roles)})", value=_lines([r.name for r in plan.roles]), inline=False)
    chans = [f"{c.name} ({_KIND_LABEL[c.kind]})" for c in plan.categories + plan.channels]
    if chans:
        embed.add_field(name=f"Channels ({len(chans)})", value=_lines(chans), inline=False)
    if plan.total:
        embed.add_field(name="What I can't bring back", value=NOT_RECOVERABLE, inline=False)
    if kick is not None and kick.possible:
        who = kick.member.mention
        embed.add_field(
            name="Kick",
            value=(
                f"**Restore + kick** also kicks {who} out of this server (they can rejoin with a new invite). "
                "**Restore only** leaves them here."
                if plan.total
                else f"**Kick them** removes {who} from this server (they can rejoin with a new invite)."
            ),
            inline=False,
        )
    elif kick is not None and kick.problem:
        embed.add_field(name="Kick (not possible)", value=f"I can't kick them: {kick.problem}.", inline=False)
    notes = []
    if plan.skipped:
        notes.append(f"{plan.skipped} item(s) already exist again and are left alone.")
    if plan.truncated:
        notes.append(f"Only the first {MAX_ITEMS} items are restored per run. Run /fix again for the rest.")
    if notes:
        embed.set_footer(text=" ".join(notes))
    return embed


def progress_embed(done: int, total: int) -> discord.Embed:
    return embeds.info("Restoring…", f"Created {done} of {total} so far. This takes a little while, so please wait.")


def result_embed(
    guild: discord.Guild, target_id: int, plan: Plan, result: Result, kick_note: str | None = None
) -> discord.Embed:
    problems = len(result.roles_failed) + len(result.channels_failed)
    if plan.total == 0:  # kick only
        embed = embeds.base(embeds.SUCCESS, "Done", f"Finished in **{guild.name}** (<@{target_id}> deleted things).")
        if kick_note:
            embed.add_field(name="Kick", value=kick_note, inline=False)
        embed.add_field(name="Remove my extra permissions", value=HOW_TO_REMOVE, inline=False)
        return embed
    embed = embeds.base(
        embeds.WARN if problems else embeds.SUCCESS,
        "Restore finished with problems" if problems else "Restore finished",
        f"I rebuilt what I could in **{guild.name}** based on what <@{target_id}> deleted.",
    )
    if kick_note:
        embed.add_field(name="Kick", value=kick_note, inline=False)
    embed.add_field(
        name="Roles",
        value=f"{len(result.roles_ok)} of {len(plan.roles)} created",
        inline=True,
    )
    embed.add_field(
        name="Channels",
        value=f"{len(result.channels_ok)} of {len(plan.categories) + len(plan.channels)} created",
        inline=True,
    )
    if result.roles_ok or result.channels_ok:
        embed.add_field(
            name="Recreated",
            value=truncate(_lines(result.roles_ok + result.channels_ok, 25), 1000),
            inline=False,
        )
    if problems:
        failed = [f"{n}: {why}" for n, why in result.roles_failed + result.channels_failed]
        embed.add_field(name="Could not create", value=truncate(_lines(failed, 8), 1000), inline=False)
    if result.notes:
        embed.add_field(name="Notes", value=truncate("\n".join(result.notes), 1000), inline=False)
    embed.add_field(name="Now it's your turn", value=NOT_RECOVERABLE, inline=False)
    embed.add_field(name="Remove my extra permissions", value=HOW_TO_REMOVE, inline=False)
    return embed


def missing_embed(
    missing: list[tuple[str, str]], plan: Plan | None, restore_link: str | None
) -> tuple[discord.Embed, discord.ui.View | None]:
    granted = link_permissions(restore_link)
    manual = [label for attr, label in missing if granted is None or not getattr(granted, attr)]

    found = (
        f"I found **{len(plan.roles)}** role(s) and **{len(plan.categories) + len(plan.channels)}** channel(s) "
        "to restore, but I can't create them yet."
        if plan is not None
        else "I can't read the audit log yet."
    )
    embed = embeds.error("I need more permissions for this", found)
    embed.add_field(name="Missing", value="\n".join(f"• {label}" for _, label in missing), inline=False)
    steps = (
        "**1.** Add me again with the button below. It updates my permissions, you don't need to kick me first.\n"
        "**2.** Run `/fix` again.\n"
        f"**3.** When the restore is done, remove these from my role: **{RISKY_TEXT}**. "
        "I'll DM you every day until they are gone."
    )
    embed.add_field(name="What to do", value=steps, inline=False)
    if manual:
        embed.add_field(
            name="Not covered by that link",
            value="Turn these on yourself (Server Settings > Roles > my role): " + ", ".join(f"**{m}**" for m in manual),
            inline=False,
        )
    view = embeds.link_view(("Re-add me with restore permissions", restore_link, None)) if restore_link else None
    return embed, view


def risky_present(guild: discord.Guild) -> list[str]:
    """Which of the risky permissions the bot currently holds in this server."""
    if guild.me is None:
        return []
    perms = guild.me.guild_permissions
    return [label for attr, label in RISKY_PERMISSIONS if getattr(perms, attr)]


def decide_reminder(row, risky: list[str], now: float) -> str:
    """send | thanks | delete | wait, for one tracked server."""
    if not risky:
        if row["sent_count"] > 0:
            return "thanks"  # they listened to a reminder: say thanks once, then stop
        if row["restored_at"] or now - row["started_at"] > WAIT_BEFORE_GIVING_UP:
            return "delete"
        return "wait"  # /fix started but the extra permissions haven't been added yet
    base = row["last_sent_at"] or row["restored_at"] or row["started_at"]
    return "send" if now - base >= REMINDER_EVERY else "wait"


def reminder_embed(guild_name: str, risky: list[str]) -> discord.Embed:
    embed = embeds.warning(
        "Please remove my extra permissions",
        f"In **{guild_name}** I still have permissions I only need for `/fix`:\n"
        + "\n".join(f"• {p}" for p in risky)
        + "\n\nIf you are done restoring, take them away so nothing can go wrong if I'm ever misused. "
        "I'll DM you once a day until they are gone.",
    )
    embed.add_field(name="How", value="Server Settings > Roles > my role > switch those permissions off.", inline=False)
    embed.add_field(
        name="Still need to restore something?",
        value="Run `/fix` first, then remove the permissions.",
        inline=False,
    )
    return embed


# --- the cog ------------------------------------------------------------------------

def parse_user_id(text: str | None) -> int | None:
    if not text:
        return None
    match = re.fullmatch(r"\s*(?:<@!?(\d{15,21})>|(\d{15,21}))\s*", text)
    return int(match.group(1) or match.group(2)) if match else None


class ConfirmRestore(discord.ui.View):
    def __init__(self, cog: "Restore", guild: discord.Guild, invoker, target_id: int, plan: Plan, kick: KickInfo) -> None:
        super().__init__(timeout=180)
        self.cog, self.guild, self.invoker, self.target_id, self.plan = cog, guild, invoker, target_id, plan
        self.interaction: discord.Interaction | None = None
        # Only offer the buttons that make sense for this situation.
        if plan.total == 0:
            self.remove_item(self.restore)
            self.remove_item(self.restore_kick)
        else:
            self.remove_item(self.kick_only)
            if kick.possible:
                self.restore.label = "Restore only"
            else:
                self.remove_item(self.restore_kick)

    async def go(self, interaction: discord.Interaction, kick: bool) -> None:
        self.stop()
        first = progress_embed(0, self.plan.total) if self.plan.total else embeds.info("Working…", "One moment.")
        await interaction.response.edit_message(embed=first, view=None)
        await self.cog.execute(interaction, self.guild, self.invoker, self.target_id, self.plan, kick)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.invoker.id:
            return True
        await interaction.response.send_message(
            embed=embeds.error("Not yours", "Only the person who ran /fix can press this."), ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                embed=embeds.warning("Timed out", "Nothing was created. Run /fix again when you're ready."), view=None
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Restore now", emoji="🛠️", style=discord.ButtonStyle.success)
    async def restore(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.go(interaction, kick=False)

    @discord.ui.button(label="Restore + kick", emoji="🥾", style=discord.ButtonStyle.danger)
    async def restore_kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.go(interaction, kick=True)

    @discord.ui.button(label="Kick them", emoji="🥾", style=discord.ButtonStyle.danger)
    async def kick_only(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.go(interaction, kick=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=embeds.info("Cancelled", "Nothing was created."), view=None)


class Restore(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.active: set[int] = set()  # guilds with a restore running

    async def cog_load(self) -> None:
        self.perm_reminders.start()

    async def cog_unload(self) -> None:
        self.perm_reminders.cancel()

    # --- /fix -----------------------------------------------------------------

    @app_commands.command(name="fix", description="Rebuild what a user deleted (audit log), and optionally kick them")
    @app_commands.describe(user_id="Whose deletions to undo. Leave empty to use the default.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def fix(self, interaction: discord.Interaction, user_id: str | None = None) -> None:
        guild = interaction.guild
        target_id = parse_user_id(user_id) if user_id else config.FIX_DEFAULT_USER_ID
        if target_id is None:
            problem = (
                "That doesn't look like a user ID. Paste the number (or @mention) of the person."
                if user_id
                else "Tell me whose deletions to undo: run `/fix user_id:<their ID>`."
            )
            await interaction.response.send_message(embed=embeds.error("Which user?", problem), ephemeral=True)
            return
        if guild.id in self.active:
            await interaction.response.send_message(
                embed=embeds.error("Already running", "A restore is already running in this server."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if not guild.me.guild_permissions.view_audit_log:
            await self.reply_missing(interaction, [("view_audit_log", "View Audit Log")], None)
            return
        try:
            roles, channels = await collect(guild, target_id)
        except discord.Forbidden:
            await self.reply_missing(interaction, [("view_audit_log", "View Audit Log")], None)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=embeds.error("Couldn't read the audit log", f"Discord said: `{_why(exc)}`"), ephemeral=True
            )
            return

        plan = build_plan(guild, roles, channels)
        evidence = len(roles) + len(channels)  # the audit log shows this user really deleted things
        kick = await check_kick(guild, interaction.user, target_id) if evidence else KickInfo()

        if plan.total == 0 and not kick.possible:
            if not evidence:
                detail = "The audit log has no deleted channels or roles from that user (Discord only keeps about 45 days)."
            else:
                detail = f"All {plan.skipped} deleted item(s) already exist again."
                if kick.problem:
                    detail += f" I can't kick them: {kick.problem}."
            await interaction.followup.send(embed=embeds.info("Nothing to restore", detail), ephemeral=True)
            return

        if plan.total:
            missing = permissions_missing(plan, guild.me.guild_permissions)
            if missing:
                await self.reply_missing(interaction, missing, plan)
                return

        # The bot holds the extra permissions now: start the "please remove them afterwards" reminders.
        await db.track_perm_reminder(guild.id, interaction.user.id)
        view = ConfirmRestore(self, guild, interaction.user, target_id, plan, kick)
        await interaction.followup.send(embed=preview_embed(target_id, plan, kick), view=view, ephemeral=True)
        view.interaction = interaction

    async def reply_missing(self, interaction: discord.Interaction, missing, plan: Plan | None) -> None:
        # The admin is being told to give the bot extra permissions: remind them to take them away again.
        await db.track_perm_reminder(interaction.guild.id, interaction.user.id)
        embed, view = missing_embed(missing, plan, config.RESTORE_INVITE_URL)
        kwargs = {"view": view} if view is not None else {}
        await interaction.followup.send(embed=embed, ephemeral=True, **kwargs)

    async def kick_target(self, guild: discord.Guild, invoker, target_id: int) -> str:
        """Kick the user being cleaned up after. Everything is checked again first: things may have changed."""
        info = await check_kick(guild, invoker, target_id)
        if not info.possible:
            return f"I could not kick <@{target_id}>: {info.problem}."
        reason = f"/fix by {invoker} ({invoker.id}): kicking {target_id}, who deleted roles or channels here"[:480]
        try:
            await info.member.kick(reason=reason)
        except discord.HTTPException as exc:
            return f"I could not kick <@{target_id}>: {_why(exc)}."
        log.warning("/fix in %s (%s): %s kicked %s", guild.name, guild.id, invoker.id, target_id)
        return f"Kicked <@{target_id}> from the server. They can rejoin with a new invite."

    async def execute(
        self, interaction: discord.Interaction, guild, invoker, target_id: int, plan: Plan, kick: bool = False
    ) -> None:
        reason = f"/fix by {invoker} ({invoker.id}): restoring things {target_id} deleted"[:480]
        self.active.add(guild.id)

        async def progress(done: int, total: int) -> None:
            try:
                await interaction.edit_original_response(embed=progress_embed(done, total))
            except discord.HTTPException:
                pass

        try:
            # Kick first: it stops the person from deleting more while the restore runs.
            kick_note = await self.kick_target(guild, invoker, target_id) if kick else None
            result = await apply(guild, plan, reason, progress) if plan.total else Result()
        finally:
            self.active.discard(guild.id)

        await db.mark_restored(guild.id)
        log.warning(
            "/fix in %s (%s) by %s for target %s: %d roles, %d channels created",
            guild.name, guild.id, invoker.id, target_id, len(result.roles_ok), len(result.channels_ok),
        )

        report = result_embed(guild, target_id, plan, result, kick_note)
        try:
            await invoker.send(embed=report)  # the DM that says what to re-organize / re-permission
            dm_note = None
        except discord.HTTPException:
            dm_note = "I couldn't DM you (your DMs are closed), so keep the instructions on this message."
        if dm_note:
            report.set_footer(text=dm_note)
        else:
            report.set_footer(text=f"{embeds.FOOTER}  •  I also sent this to your DMs")
        try:
            await interaction.edit_original_response(embed=report, view=None)
        except discord.HTTPException:
            pass

    # --- daily "remove my extra permissions" reminders -------------------------

    @tasks.loop(hours=1)
    async def perm_reminders(self) -> None:
        try:
            for row in await db.list_perm_reminders():
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    await db.delete_perm_reminder(row["guild_id"])
                    continue
                risky = risky_present(guild)
                action = decide_reminder(row, risky, time.time())
                if action == "wait":
                    continue
                if action == "delete":
                    await db.delete_perm_reminder(guild.id)
                elif action == "thanks":
                    await self.dm_admin(row, guild, embeds.success("All clear", f"My extra permissions are gone from **{guild.name}**. Thank you!"))
                    await db.delete_perm_reminder(guild.id)
                else:
                    await self.dm_admin(row, guild, reminder_embed(guild.name, risky))
                    await db.mark_perm_reminder_sent(guild.id)
                await asyncio.sleep(1)
        except Exception:
            log.exception("Permission reminder loop crashed")

    @perm_reminders.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    async def dm_admin(self, row, guild: discord.Guild, embed: discord.Embed) -> bool:
        """DM the admin who ran /fix, falling back to the server owner."""
        for user_id in dict.fromkeys(filter(None, [row["user_id"], guild.owner_id])):
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
                return True
            except discord.HTTPException as exc:
                log.warning("Couldn't DM %s about permissions in %s: %s", user_id, guild.id, exc)
        return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Restore(bot))
