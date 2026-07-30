import os
from typing import Any

from config import PROJECT_ROOT
from core.storage import JsonStore

SETTINGS_FILE = PROJECT_ROOT / "settings.json"
SECRETS_FILE = PROJECT_ROOT / "data" / "secrets.json"

_settings_store = JsonStore(SETTINGS_FILE, dict)
_secrets_store = JsonStore(SECRETS_FILE, dict)


def load_settings() -> dict[str, Any]:
    return _settings_store.read()


def save_settings(data: dict[str, Any]) -> None:
    _settings_store.write(data)


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    def update(data: dict[str, Any]) -> dict[str, Any]:
        data[key] = value
        return data

    _settings_store.update(update)


def delete_setting(key: str) -> None:
    def update(data: dict[str, Any]) -> dict[str, Any]:
        data.pop(key, None)
        return data

    _settings_store.update(update)


def get_secret(key: str, default: str | None = None) -> str | None:
    """Read secrets from ignored local storage, then environment, then legacy settings."""
    def usable(value: Any) -> str | None:
        if not value:
            return None
        text = str(value).strip()
        if text.startswith("your_") and text.endswith("_here"):
            return None
        return text

    value = usable(_secrets_store.read().get(key))
    if value is not None:
        return value

    value = usable(os.getenv(key))
    if value is not None:
        return value

    # Compatibility with keys saved by older bot versions.
    return usable(get_setting(key)) or default


def set_secret(key: str, value: str) -> None:
    def update(data: dict[str, Any]) -> dict[str, Any]:
        data[key] = value
        return data

    _secrets_store.update(update)
    if key in load_settings():
        delete_setting(key)
