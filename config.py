import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "America/Toronto")
TZ = ZoneInfo(BOT_TIMEZONE)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def get_env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable after loading the project-local .env file."""
    return os.getenv(name, default)
