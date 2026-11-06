#!/usr/bin/env python3
"""
check_safety.py - a tripwire for "is this Discord bot a nuke bot / token stealer / backdoor?"

    python tools/check_safety.py            scan the project this file lives in
    python tools/check_safety.py PATH       scan another folder
    python tools/check_safety.py --quiet    only the summary and anything unexpected

It reads the code with Python's `ast` module (it never runs it) and lists, with file and line:
  * every Discord call that can change a server (create / delete / leave / pin ...)
  * every moderation call (ban / kick / timeout / role changes / purge ...): a bot like this should have NONE
  * code execution, network and file access (eval, exec, subprocess, requests, sockets, open ...)
  * every web address, every place the bot token is touched, every hard-coded Discord ID
  * suspicious blobs of encoded text, unexpected files, unexpected dependencies

Every finding is compared with a short, readable list of what this project is EXPECTED to contain
(the EXPECTED table below, each entry with its reason). Anything else is reported as UNEXPECTED
and the exit code is 1.

IMPORTANT - what this is and is not:
  * It is a tripwire, not a proof. Clever or obfuscated code can slip past any pattern scanner.
  * If you got this file from the same place as the code, a malicious author could have edited this
    checker too. Read the EXPECTED table (it is short), compare the files with a copy you trust,
    and ALSO run the AI review described in the README.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------------------------
# What this project is expected to contain. (rule, name) -> ({files allowed to do it}, reason)
# Anything not listed here, or found in a different file, is reported as UNEXPECTED.
# ---------------------------------------------------------------------------------------------
EXPECTED: dict[tuple[str, str], tuple[set[str], str]] = {
    # Discord actions that change a server. Only /fix creates things, and only after an admin confirms.
    ("discord-action", "create_role"): ({"cogs/restore.py"}, "/fix recreates a deleted role (no permissions)"),
    ("discord-action", "create_text_channel"): ({"cogs/restore.py"}, "/fix recreates a deleted channel (hidden from @everyone)"),
    ("discord-action", "create_voice_channel"): ({"cogs/restore.py"}, "/fix recreates a deleted channel (hidden from @everyone)"),
    ("discord-action", "create_stage_channel"): ({"cogs/restore.py"}, "/fix recreates a deleted channel (hidden from @everyone)"),
    ("discord-action", "create_category"): ({"cogs/restore.py"}, "/fix recreates a deleted category (hidden from @everyone)"),
    ("discord-action", "create_forum"): ({"cogs/restore.py"}, "/fix recreates a deleted forum (hidden from @everyone)"),
    ("discord-action", "audit_logs"): ({"cogs/restore.py"}, "/fix reads the audit log (read only)"),
    # The only moderation action: /fix may kick the user whose deletions it is repairing.
    ("moderation", "kick"): (
        {"cogs/restore.py"},
        "/fix can kick the user it is cleaning up after: admin-only, its own button, never the owner / the admin / the bot / anyone ranked above them",
    ),
    ("discord-action", "delete"): ({"cogs/moderation.py"}, "removes the bot's OWN posted ad messages after a ban"),
    ("discord-action", "leave"): ({"main.py", "cogs/jobs.py"}, "bot leaves a server the owner kicked / banned"),
    ("discord-action", "pin"): ({"cogs/delivery.py"}, "premium featured post: pins the bot's own message"),
    ("discord-action", "unpin"): ({"cogs/delivery.py"}, "premium featured post: unpins the bot's previous pin"),
    # Tokens and settings
    ("env-access", "getenv"): ({"config.py"}, "reads settings from .env"),
    ("env-access", "environ"): ({"start.py"}, "start.py hands the environment to the two bot processes"),
    ("token-ref", "token"): (
        {"config.py", "main.py", "helper_bot/main.py", "start.py"},
        "reads the token and passes it to the library's .run()",
    ),
    # Processes and files
    ("import", "subprocess"): ({"start.py"}, "start.py launches the main bot and the helper bot"),
    ("call", "subprocess.Popen"): ({"start.py"}, "start.py launches the main bot and the helper bot"),
    ("file", "mkdir"): ({"db.py", "cogs/backup.py"}, "creates the data/ and backups/ folders"),
    ("file", "unlink"): ({"cogs/backup.py"}, "deletes the oldest database backups"),
    ("file", "write_text"): ({"tools/make_review_bundle.py"}, "writes the review bundle you ask it to make"),
}

# Web hosts that may appear in the code. Everything else is UNEXPECTED.
ALLOWED_HOSTS = {"discord.com", "discord.gg", "discordapp.com", "www.discord.com"}

# Dependencies the project is expected to need.
ALLOWED_DEPENDENCIES = {"discord.py", "aiosqlite", "python-dotenv", "psutil"}

# Discord API calls that should NOT appear anywhere in a partnership bot.
MODERATION_CALLS = {
    "ban", "unban", "kick", "timeout", "bulk_ban", "prune_members", "edit_role_positions", "add_roles",
    "remove_roles", "delete_messages", "purge", "delete_invites", "edit_member", "move_to", "create_webhook",
    "create_invite", "create_thread", "change_voice_state",
}
# Discord calls that change a server and are only allowed where EXPECTED says so.
ACTION_CALLS = {
    "create_role", "create_text_channel", "create_voice_channel", "create_stage_channel", "create_category",
    "create_forum", "audit_logs", "delete", "leave", "pin", "unpin", "edit",
}
DANGEROUS_BUILTINS = {"eval", "exec", "compile", "__import__"}
DANGEROUS_DOTTED = {
    "os.system", "os.popen", "os.startfile", "os.execv", "os.execve", "os.spawnl", "os.fork",
    "subprocess.run", "subprocess.call", "subprocess.check_output", "subprocess.check_call", "subprocess.Popen",
    "importlib.import_module", "shutil.rmtree", "shutil.move", "shutil.copy",
}
DANGEROUS_IMPORTS = {
    "subprocess", "socket", "socketserver", "requests", "httpx", "urllib3", "aiohttp", "http.client",
    "urllib.request", "ftplib", "smtplib", "telnetlib", "ctypes", "pickle", "marshal", "webbrowser", "pty",
    "paramiko", "selenium", "pyautogui", "keyboard", "pynput", "win32api", "win32com", "winreg", "base64",
    "zlib", "bz2", "lzma", "codecs", "binascii", "shutil",
}
FILE_CALLS = {"open", "unlink", "rmtree", "rmdir", "remove", "rename", "write_text", "write_bytes", "chmod", "mkdir", "touch"}
TOKEN_NAMES = {"TOKEN", "HELPER_TOKEN", "DISCORD_TOKEN", "HELPER_DISCORD_TOKEN"}
TOKEN_SAFE_CALLEES = {"run", "bool", "getenv", "strip"}

# Files that must never be shipped, and the only file types a bot like this needs.
FORBIDDEN_FILES = {".env", "token.txt", "tokens.txt", "config.json", "secrets.json"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".rar", ".zip", ".7z", ".exe", ".dll", ".bat",
                      ".cmd", ".ps1", ".sh", ".vbs", ".jar", ".so", ".dylib", ".pyc", ".pyd"}
ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".example", ".gitignore"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "data", "backups"}
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")


class Finding(NamedTuple):
    rule: str
    name: str
    path: str
    line: int
    detail: str = ""


# ---------------------------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------------------------

def dotted(node: ast.AST) -> str | None:
    """'os.system' for the expression os.system, or None if it is not a plain dotted name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def mentions_token(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in TOKEN_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in TOKEN_NAMES:
            return True
    return False


class Scanner(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.found: list[Finding] = []
        self.commands: list[tuple[str, int]] = []
        self._seen: set[Finding] = set()

    def add(self, rule: str, name: str, node: ast.AST, detail: str = "") -> None:
        # The helper tools mention the token names only in order to look for them.
        if rule == "token-ref" and self.rel.startswith("tools/"):
            return
        finding = Finding(rule, name, self.rel, getattr(node, "lineno", 0), detail)
        if finding not in self._seen:  # one line can touch the same thing twice
            self._seen.add(finding)
            self.found.append(finding)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._import(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self._import(module, node)
        for alias in node.names:  # `from os import system`, `from urllib import request` ...
            self._import(f"{module}.{alias.name}" if module else alias.name, node)

    def _import(self, module: str, node: ast.AST) -> None:
        for dangerous in DANGEROUS_IMPORTS:
            if module == dangerous or module.startswith(dangerous + "."):
                self.add("import", dangerous, node)
                break

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        full = dotted(func)

        if isinstance(func, ast.Name) and func.id in DANGEROUS_BUILTINS:
            self.add("call", func.id, node)
        if full in DANGEROUS_DOTTED:
            self.add("call", full, node)
        if isinstance(func, ast.Attribute):
            if name in MODERATION_CALLS:
                self.add("moderation", name, node)
            elif name in ACTION_CALLS:
                self.add("discord-action", name, node)
        if name in FILE_CALLS and (isinstance(func, ast.Attribute) or name == "open"):
            self.add("file", name, node)
        if full in ("os.getenv",) or name == "getenv":
            self.add("env-access", "getenv", node)

        # Is a token handed to something other than the library's run()?
        if name not in TOKEN_SAFE_CALLEES:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if mentions_token(arg):
                    self.add("token-flow", name or "?", node, "a token is passed to this call")
                    break

        # Slash commands defined here (for the command inventory)
        if name == "command" and full and full.endswith("app_commands.command"):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    self.commands.append((str(kw.value.value), node.lineno))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "environ":
            self.add("env-access", "environ", node)
        if node.attr in TOKEN_NAMES:
            self.add("token-ref", "token", node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in TOKEN_NAMES:
            self.add("token-ref", "token", node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, str):
            if value in TOKEN_NAMES:
                self.add("token-ref", "token", node)
            for host in re.findall(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", value):
                self.add("url", host.lower(), node)
            if len(value) > 400 and not re.search(r"\s", value):
                self.add("blob", f"{len(value)} chars", node, "long string with no spaces (encoded data?)")
            if re.fullmatch(r"\d{17,20}", value.strip()):
                self.add("snowflake", value.strip(), node)
        elif isinstance(value, int) and not isinstance(value, bool) and len(str(value)) >= 17:
            self.add("snowflake", str(value), node)

def non_ascii_identifiers(tree: ast.AST) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(tree):
        for ident in (getattr(node, "id", None), getattr(node, "name", None), getattr(node, "attr", None)):
            if isinstance(ident, str) and not ident.isascii():
                out.append((ident, getattr(node, "lineno", 0)))
    return out


def scan_tree(root: Path) -> tuple[list[Finding], list[tuple[str, str, int]], list[str]]:
    findings: list[Finding] = []
    commands: list[tuple[str, str, int]] = []
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: cannot be parsed as Python ({exc.__class__.__name__}) - read it by hand")
            continue
        scanner = Scanner(rel)
        scanner.visit(tree)
        findings.extend(scanner.found)
        commands.extend((cmd, rel, line) for cmd, line in scanner.commands)
        for ident, line in non_ascii_identifiers(tree):
            findings.append(Finding("obfuscation", ident, rel, line, "non-ASCII identifier"))
    return findings, commands, problems


def scan_files(root: Path) -> list[str]:
    bad = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        if path.name in FORBIDDEN_FILES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            bad.append(f"{rel}: this kind of file should not be in a bot's source (secrets / binaries / scripts)")
        elif path.suffix.lower() not in ALLOWED_SUFFIXES and path.name not in {".gitignore", ".env.example"}:
            bad.append(f"{rel}: unexpected file type")
        elif path.suffix.lower() in {".py", ".txt", ".md"} and b"\x00" in path.read_bytes()[:4096]:
            bad.append(f"{rel}: contains binary data")
        elif path.suffix.lower() in ALLOWED_SUFFIXES or path.name in {".gitignore", ".env.example"}:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            if TOKEN_SHAPE.search(text):
                bad.append(f"{rel}: contains something shaped like a Discord bot token")
    return bad


def scan_requirements(root: Path) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    req = root / "requirements.txt"
    if not req.exists():
        return ok, ["requirements.txt is missing"]
    for raw in req.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower().replace("_", "-")
        pinned = "==" in line
        if name in ALLOWED_DEPENDENCIES:
            ok.append(f"{line}" + ("" if pinned else "   (not pinned to an exact version)"))
        else:
            bad.append(f"{line}: not an expected dependency")
    return ok, bad


# ---------------------------------------------------------------------------------------------
# Judging + printing
# ---------------------------------------------------------------------------------------------

def judge(f: Finding) -> tuple[bool, str]:
    """(expected?, explanation)"""
    if f.rule == "url":
        return (f.name in ALLOWED_HOSTS, "a Discord address" if f.name in ALLOWED_HOSTS else "NOT a Discord address")
    if f.rule == "moderation" and (f.rule, f.name) not in EXPECTED:
        return False, "a moderation / abuse-capable call; this bot is not supposed to have it"
    if f.rule in {"token-flow", "blob", "snowflake", "obfuscation"}:
        return False, {
            "token-flow": "a token is handed to something other than the library's run()",
            "blob": "possible encoded payload",
            "snowflake": "a hard-coded Discord ID (backdoor / special-privilege risk)",
            "obfuscation": "looks obfuscated",
        }[f.rule]
    allowed = EXPECTED.get((f.rule, f.name))
    if allowed is None:
        return False, "not on the expected list"
    paths, reason = allowed
    if f.path in paths:
        return True, reason
    return False, f"allowed only in {', '.join(sorted(paths))}"


SECTIONS = [
    ("Discord actions that change a server", {"discord-action"}),
    ("Moderation / abuse-capable calls (only the optional kick in /fix is expected)", {"moderation"}),
    ("Code execution, processes, network libraries, encoding", {"call", "import"}),
    ("File access", {"file"}),
    ("Settings and tokens", {"env-access", "token-ref", "token-flow"}),
    ("Web addresses in the code", {"url"}),
    ("Hard-coded Discord IDs (there should be NONE)", {"snowflake"}),
    ("Obfuscation signs (there should be NONE)", {"blob", "obfuscation"}),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tripwire scan for nuke-bot / stealer / backdoor patterns.")
    parser.add_argument("path", nargs="?", default=None, help="folder to scan (default: the project this file is in)")
    parser.add_argument("--quiet", action="store_true", help="only print the summary and unexpected findings")
    args = parser.parse_args(argv)
    # Odd characters in a file name or identifier must show up in the report, never crash it.
    sys.stdout.reconfigure(errors="backslashreplace")

    root = Path(args.path).resolve() if args.path else Path(__file__).resolve().parent.parent
    if not root.is_dir():
        print(f"not a folder: {root}")
        return 2

    findings, commands, problems = scan_tree(root)
    bad_files = scan_files(root)
    deps_ok, deps_bad = scan_requirements(root)

    unexpected = 0
    expected = 0
    print(f"check_safety: scanning {root}\n")

    for title, rules in SECTIONS:
        rows = [f for f in findings if f.rule in rules]
        print(f"== {title} ==")
        if not rows:
            print("   [ok] none found")
        by_key: dict[str, list[Finding]] = defaultdict(list)
        for f in rows:
            by_key[f"{f.rule}:{f.name}"].append(f)
        for key in sorted(by_key):
            for f in by_key[key]:
                good, why = judge(f)
                expected += good
                unexpected += not good
                if good and args.quiet:
                    continue
                tag = "[ok]" if good else "[!!]"
                extra = f"  ({f.detail})" if f.detail else ""
                print(f"   {tag} {f.name:<22} {f.path}:{f.line}   {why}{extra}")
        print()

    print("== Slash commands defined ==")
    for cmd, rel, line in sorted(commands):
        print(f"   /{cmd:<16} {rel}:{line}")
    if not commands:
        print("   none")
    print()

    print("== Dependencies (requirements.txt) ==")
    for line in deps_ok:
        print(f"   [ok] {line}")
    for line in deps_bad:
        print(f"   [!!] {line}")
        unexpected += 1
    print()

    print("== Files ==")
    if not bad_files and not problems:
        print("   [ok] only source / text files, no secrets or binaries")
    for line in [*bad_files, *problems]:
        print(f"   [!!] {line}")
        unexpected += 1
    print()

    if unexpected:
        print(f"RESULT: {unexpected} UNEXPECTED finding(s), {expected} expected. Read every [!!] line before running this.")
        return 1
    print(f"RESULT: no unexpected findings ({expected} expected ones listed above, each with its reason).")
    print("This is a tripwire, not a proof. Also read the code and run the AI review from the README.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
