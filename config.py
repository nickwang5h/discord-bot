import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _resolve_state_root(value: str | None) -> Path:
    if not value or not value.strip():
        return PROJECT_ROOT
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("BOT_STATE_DIR 必须是绝对路径")
    return path


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"布尔环境变量值无效: {value!r}")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "America/Toronto")
TZ = ZoneInfo(BOT_TIMEZONE)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
STATE_ROOT = _resolve_state_root(os.getenv("BOT_STATE_DIR"))
SCHEDULED_JOBS_ENABLED = _parse_bool(
    os.getenv("BOT_ENABLE_SCHEDULED_JOBS"),
    default=True,
)
BOT_RELEASE = (os.getenv("BOT_RELEASE") or "dev").replace("\n", " ")[:64]


def get_env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable after loading the project-local .env file."""
    return os.getenv(name, default)
