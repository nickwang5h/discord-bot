#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str]) -> int:
    print(f"\n$ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bot's local validation suite")
    parser.add_argument("--live", action="store_true", help="Also verify external model catalogs and Discord token")
    parser.add_argument(
        "--allow-missing-secrets",
        action="store_true",
        help="Allow CI to validate code without deployment secrets",
    )
    args = parser.parse_args()

    commands = [
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "bot.py",
            "config.py",
            "core",
            "cogs",
            "scripts",
            "tests",
        ],
        [sys.executable, "-m", "unittest", "discover", "-v", "-s", "tests"],
    ]

    commands.append([sys.executable, "scripts/healthcheck.py"])
    if not args.allow_missing_secrets:
        commands[-1].append("--strict")
    if args.live:
        commands[-1].append("--live")

    for command in commands:
        exit_code = run(command)
        if exit_code:
            return exit_code
    print("\n✅ Project validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
