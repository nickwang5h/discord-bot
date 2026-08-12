from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import MutableMapping


DEFAULT_RUNTIME_ENV = Path("/root/.config/discord-bot/runtime.env")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_MAX_ENV_BYTES = 64 * 1024


class RuntimeEnvError(RuntimeError):
    pass


def _decode_value(raw: str, *, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise RuntimeEnvError(f"runtime env line {line_number} is invalid")
        return value[1:-1]
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise RuntimeEnvError(f"runtime env line {line_number} is invalid") from error
        if not isinstance(decoded, str):
            raise RuntimeEnvError(f"runtime env line {line_number} is invalid")
        return decoded
    if "\x00" in value:
        raise RuntimeEnvError(f"runtime env line {line_number} is invalid")
    return value


def load_runtime_env_file(
    path: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
    strict_parent: bool = True,
) -> tuple[str, ...]:
    target = os.environ if environ is None else environ
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeEnvError("runtime env metadata is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > _MAX_ENV_BYTES
    ):
        raise RuntimeEnvError("runtime env must be an owner-only regular file")
    if strict_parent:
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeEnvError("runtime env directory must be owner-only")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeEnvError("runtime env is not readable UTF-8") from error
    loaded: list[str] = []
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeEnvError(f"runtime env line {line_number} is invalid")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise RuntimeEnvError(f"runtime env line {line_number} is invalid")
        target.setdefault(name, _decode_value(raw_value, line_number=line_number))
        loaded.append(name)
    return tuple(loaded)


def load_default_runtime_env(
    *,
    project_root: Path,
    environ: MutableMapping[str, str] | None = None,
) -> Path | None:
    target = os.environ if environ is None else environ
    configured = target.get("DISCORD_BOT_ENV_FILE")
    if configured:
        canonical = Path(configured).expanduser()
        try:
            present = canonical.exists() or canonical.is_symlink()
        except OSError as error:
            raise RuntimeEnvError("configured runtime env is unavailable") from error
        if not present:
            raise RuntimeEnvError("configured runtime env is unavailable")
        load_runtime_env_file(canonical, environ=target, strict_parent=True)
        return canonical
    for candidate, strict_parent in (
        (DEFAULT_RUNTIME_ENV, True),
        (project_root / ".env", False),
    ):
        try:
            present = candidate.exists() or candidate.is_symlink()
        except OSError:
            continue
        if present:
            load_runtime_env_file(
                candidate, environ=target, strict_parent=strict_parent
            )
            return candidate
    return None
