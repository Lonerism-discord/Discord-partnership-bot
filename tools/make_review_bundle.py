#!/usr/bin/env python3
"""
make_review_bundle.py - build ONE text file you can paste into an AI to have this bot reviewed.

    python tools/make_review_bundle.py                    -> review_bundle.txt
    python tools/make_review_bundle.py --max-chars 90000  -> review_bundle_part1.txt, _part2.txt ... (for small chat windows)
    python tools/make_review_bundle.py --no-line-numbers

The bundle is: the review prompt (tools/ai_review_prompt.txt), a list of every file, then every file's
content with line numbers so the AI can cite exact lines. It never includes .env or anything that looks
like a bot token, and refuses to run if it finds one.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "data", "backups"}
SKIP_NAMES = {".env", "SHA256SUMS.txt"}
INCLUDE_SUFFIXES = {".py", ".md", ".txt", ".example"}
INCLUDE_NAMES = {".gitignore", ".env.example"}
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")


def project_files(skip_tools: bool) -> list[Path]:
    files = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_NAMES or path.name.startswith("review_bundle"):
            continue
        if skip_tools and rel.parts[0] == "tools":
            continue
        if path.suffix.lower() in INCLUDE_SUFFIXES or path.name in INCLUDE_NAMES:
            files.append(path)
    # code first, docs last
    return sorted(files, key=lambda p: (p.suffix != ".py", p.relative_to(ROOT).as_posix()))


def render(path: Path, line_numbers: bool) -> str:
    rel = path.relative_to(ROOT).as_posix()
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    body = "\n".join(f"{i:>5} | {line}" for i, line in enumerate(lines, 1)) if line_numbers else "\n".join(lines)
    return f"\n===================== FILE: {rel} ({len(lines)} lines) =====================\n{body}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bundle the project + review prompt into one text file.")
    parser.add_argument("--out", default="review_bundle", help="output name without extension (default: review_bundle)")
    parser.add_argument("--max-chars", type=int, default=0, help="split into parts of about this many characters")
    parser.add_argument("--no-line-numbers", action="store_true")
    parser.add_argument("--skip-tools", action="store_true", help="leave out tools/ (the checker + this script)")
    args = parser.parse_args()

    prompt_file = ROOT / "tools" / "ai_review_prompt.txt"
    if not prompt_file.exists():
        print("tools/ai_review_prompt.txt is missing")
        return 2
    prompt = prompt_file.read_text(encoding="utf-8-sig").strip()

    files = project_files(args.skip_tools)
    blocks = []
    for path in files:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if TOKEN_SHAPE.search(text):
            print(f"STOP: {path.relative_to(ROOT).as_posix()} contains something shaped like a bot token. Not bundling.")
            return 1
        blocks.append(render(path, not args.no_line_numbers))

    listing = "\n".join(
        f"  {i}. {p.relative_to(ROOT).as_posix()}" for i, p in enumerate(files, 1)
    )
    header = (
        f"{prompt}\n\n===================== FILE LIST ({len(files)} files) =====================\n"
        f"{listing}\n"
    )

    # Split on file boundaries so no file is cut in half (a single huge file stays whole).
    parts: list[str] = []
    current = header
    for block in blocks:
        if args.max_chars and len(current) + len(block) > args.max_chars and current.strip():
            parts.append(current)
            current = ""
        current += block
    parts.append(current)

    out_base = Path(args.out)
    written = []
    for i, text in enumerate(parts, 1):
        if len(parts) > 1:
            intro = "" if i == 1 else f"[This is part {i} of {len(parts)} of the same review. Continue the review with these files.]\n"
            outro = "" if i == len(parts) else f"\n[End of part {i} of {len(parts)}. More files follow in the next message.]\n"
            text = intro + text + outro
            name = out_base.with_name(f"{out_base.name}_part{i}.txt")
        else:
            name = out_base.with_name(f"{out_base.name}.txt")
        name.write_text(text, encoding="utf-8")
        written.append((name, len(text)))

    total = sum(n for _, n in written)
    for name, size in written:
        print(f"wrote {name}  ({size:,} characters)")
    print(f"\n{len(files)} files, about {total // 4:,} tokens in total.")
    print("Paste each file into your AI chat in order. The prompt is at the top of the first one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
