"""Start the main bot and the helper bot together:  python start.py   (Ctrl+C stops both)"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent

BOTS = [
    ("main  ", ROOT / "main.py", bool(config.TOKEN), "DISCORD_TOKEN"),
    ("helper", ROOT / "helper_bot" / "main.py", bool(config.HELPER_TOKEN), "HELPER_DISCORD_TOKEN"),
]


def pipe(name: str, proc: subprocess.Popen) -> None:
    """Forward a bot's output, tagged with its name."""
    for line in proc.stdout:
        print(f"[{name}] {line.rstrip()}", flush=True)


def main() -> None:
    # Bot logs can contain emoji / non-latin server names; never let printing them kill the forwarder.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs: list[tuple[str, subprocess.Popen]] = []
    for name, script, has_token, env_name in BOTS:
        if not has_token:
            print(f"[{name}] skipped - {env_name} is not set in .env", flush=True)
            continue
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            cwd=ROOT,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(target=pipe, args=(name, proc), daemon=True).start()
        procs.append((name, proc))

    if not procs:
        sys.exit("No tokens set. Copy .env.example to .env and fill it in.")

    reported: set[str] = set()
    try:
        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None and name not in reported:
                    reported.add(name)
                    print(f"[{name}] exited with code {code}", flush=True)
            if len(reported) == len(procs):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping bots...", flush=True)
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
