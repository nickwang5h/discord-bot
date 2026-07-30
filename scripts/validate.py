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
        [sys.executable, "-m", "compileall", "-q", "bot.py", "config.py", "core", "cogs", "scripts"],
    ]
    local_test_modules = [
        "scratch.test_core_services",
        "scratch.test_extensions",
        "scratch.test_regressions",
    ]
    available_test_modules = [
        module
        for module in local_test_modules
        if (PROJECT_ROOT / f"{module.replace('.', '/')}.py").is_file()
    ]
    if available_test_modules:
        commands.append(
            [sys.executable, "-m", "unittest", "-v", *available_test_modules]
        )

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
