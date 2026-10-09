"""Dollar budget, reservations and availability state for the Claude provider.

Usage lives in ``<STATE_ROOT>/data/claude_usage.json`` and is only changed through
``JsonStore.update()``. Every Claude call reserves its worst-case cost first and is
settled with the actual ``usage`` afterwards. A process that dies between the two
leaves the reservation in place, which errs on the side of spending less.
"""

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import STATE_ROOT
from core import settings
from core.storage import JsonStore

logger = logging.getLogger(__name__)

USAGE_FILE = STATE_ROOT / "data" / "claude_usage.json"

INPUT_USD_PER_TOKEN = 4.0 / 1_000_000
OUTPUT_USD_PER_TOKEN = 20.0 / 1_000_000
KEEP_DAYS = 40
KEEP_MONTHS = 13

# name: (default, minimum, maximum)
LIMIT_BOUNDS: dict[str, tuple[float, float, float]] = {
    "daily_usd": (0.60, 0.05, 1.50),
    "monthly_usd": (19.00, 1.0, 60.0),
    "daily_calls": (30, 1, 80),
}

_stores: dict[Path, JsonStore] = {}


def _empty_state() -> dict[str, Any]:
    return {
        "days": {},
        "months": {},
        "disabled_until": 0.0,
        "disabled_reason": "",
        "cooldown_until": 0.0,
    }


def _store() -> JsonStore:
    path = USAGE_FILE
    store = _stores.get(path)
    if store is None:
        store = _stores[path] = JsonStore(path, _empty_state)
    return store


def _normalized(data: Any) -> dict[str, Any]:
    state = _empty_state()
    if isinstance(data, dict):
        for key, default in state.items():
            value = data.get(key, default)
            if isinstance(default, dict):
                state[key] = value if isinstance(value, dict) else {}
            elif isinstance(default, float):
                state[key] = float(value) if isinstance(value, (int, float)) else 0.0
            else:
                state[key] = str(value) if isinstance(value, str) else ""
    return state


def _empty_day() -> dict[str, Any]:
    return {
        "reserved_usd": 0.0,
        "spent_usd": 0.0,
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "routes": {},
    }


def _day(state: dict[str, Any], day_key: str) -> dict[str, Any]:
    day = state["days"].get(day_key)
    if not isinstance(day, dict):
        day = state["days"][day_key] = _empty_day()
    for key, default in _empty_day().items():
        day.setdefault(key, default)
    return day


def day_key(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def month_key(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")


def next_utc_midnight(now: float) -> float:
    current = datetime.fromtimestamp(now, timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return midnight.timestamp()


def _prune(state: dict[str, Any], now: float) -> None:
    cutoff = day_key(now - KEEP_DAYS * 86400)
    state["days"] = {key: value for key, value in state["days"].items() if key >= cutoff}
    months = sorted(state["months"])
    for key in months[:-KEEP_MONTHS]:
        state["months"].pop(key, None)


def limits() -> dict[str, float]:
    """Public ``CLAUDE_LIMITS`` setting, clamped into the allowed range."""
    configured = settings.get_setting("CLAUDE_LIMITS", {})
    if not isinstance(configured, dict):
        logger.error("CLAUDE_LIMITS 配置无效，使用默认值")
        configured = {}
    unknown = set(configured) - set(LIMIT_BOUNDS)
    if unknown:
        logger.error("CLAUDE_LIMITS 含未知字段 %s，已忽略", sorted(unknown))
    result: dict[str, float] = {}
    for name, (default, low, high) in LIMIT_BOUNDS.items():
        value = configured.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            logger.error("CLAUDE_LIMITS.%s 无效，使用默认值", name)
            value = default
        result[name] = min(high, max(low, float(value)))
    result["daily_calls"] = int(result["daily_calls"])
    return result


def estimate_input_tokens(*parts: str) -> int:
    """Conservative token estimate: CJK 1.5 tokens/char, other text 1 token/3 chars."""
    cjk = 0
    other = 0
    for part in parts:
        for char in part or "":
            code = ord(char)
            if (
                0x3040 <= code <= 0x30FF
                or 0x3400 <= code <= 0x4DBF
                or 0x4E00 <= code <= 0x9FFF
                or 0xAC00 <= code <= 0xD7AF
                or 0xF900 <= code <= 0xFAFF
                or 0xFF00 <= code <= 0xFFEF
                or 0x20000 <= code <= 0x2FA1F
            ):
                cjk += 1
            else:
                other += 1
    return math.ceil(cjk * 1.5 + other / 3)


def worst_case_usd(input_tokens: int, max_tokens: int) -> float:
    return input_tokens * INPUT_USD_PER_TOKEN + max_tokens * OUTPUT_USD_PER_TOKEN


def actual_usd(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * INPUT_USD_PER_TOKEN + output_tokens * OUTPUT_USD_PER_TOKEN


@dataclass(frozen=True, slots=True)
class Reservation:
    route: str
    day: str
    month: str
    usd: float


def blocked_reason(now: float | None = None) -> str | None:
    """Return why Claude is unavailable right now (disabled/cooldown), else None."""
    now = time.time() if now is None else now
    state = _normalized(_store().read())
    if state["disabled_until"] > now:
        return f"停用（{state['disabled_reason'] or '未知原因'}）"
    if state["cooldown_until"] > now:
        return "冷却中"
    return None


def reserve(route: str, usd: float, route_daily_calls: int, now: float | None = None) -> Reservation | None:
    """Reserve ``usd`` for one call, or return None when any limit would be exceeded."""
    now = time.time() if now is None else now
    caps = limits()
    today = day_key(now)
    month = month_key(now)
    granted: list[Reservation] = []

    def mutate(data: Any) -> dict[str, Any]:
        state = _normalized(data)
        _prune(state, now)
        day = _day(state, today)
        committed_today = float(day["spent_usd"]) + float(day["reserved_usd"])
        committed_month = float(state["months"].get(month, 0.0)) + float(day["reserved_usd"])
        if committed_today + usd > caps["daily_usd"]:
            return state
        if committed_month + usd > caps["monthly_usd"]:
            return state
        if int(day["calls"]) >= caps["daily_calls"]:
            return state
        routes = day["routes"] if isinstance(day["routes"], dict) else {}
        if int(routes.get(route, 0)) >= route_daily_calls:
            return state
        day["reserved_usd"] = float(day["reserved_usd"]) + usd
        day["calls"] = int(day["calls"]) + 1
        routes[route] = int(routes.get(route, 0)) + 1
        day["routes"] = routes
        granted.append(Reservation(route, today, month, usd))
        return state

    _store().update(mutate)
    return granted[0] if granted else None


def settle(reservation: Reservation, input_tokens: int = 0, output_tokens: int = 0) -> float:
    """Release a reservation and book the actual cost. Returns the booked USD."""
    cost = actual_usd(max(0, input_tokens), max(0, output_tokens))

    def mutate(data: Any) -> dict[str, Any]:
        state = _normalized(data)
        day = _day(state, reservation.day)
        day["reserved_usd"] = max(0.0, float(day["reserved_usd"]) - reservation.usd)
        day["spent_usd"] = float(day["spent_usd"]) + cost
        day["input_tokens"] = int(day["input_tokens"]) + max(0, input_tokens)
        day["output_tokens"] = int(day["output_tokens"]) + max(0, output_tokens)
        state["months"][reservation.month] = float(state["months"].get(reservation.month, 0.0)) + cost
        return state

    _store().update(mutate)
    return cost


def disable_until(until: float, reason: str) -> None:
    def mutate(data: Any) -> dict[str, Any]:
        state = _normalized(data)
        state["disabled_until"] = max(state["disabled_until"], until)
        state["disabled_reason"] = reason
        return state

    _store().update(mutate)


def cooldown_until(until: float) -> None:
    def mutate(data: Any) -> dict[str, Any]:
        state = _normalized(data)
        state["cooldown_until"] = max(state["cooldown_until"], until)
        return state

    _store().update(mutate)


def status(now: float | None = None) -> dict[str, Any]:
    """Numbers and state only; safe for /health and diagnostics."""
    now = time.time() if now is None else now
    state = _normalized(_store().read())
    caps = limits()
    day = state["days"].get(day_key(now))
    day = day if isinstance(day, dict) else _empty_day()
    spent_today = float(day.get("spent_usd", 0.0))
    reserved_today = float(day.get("reserved_usd", 0.0))
    month_spent = float(state["months"].get(month_key(now), 0.0))
    disabled = state["disabled_until"] > now
    return {
        "today_usd": round(spent_today, 4),
        "today_reserved_usd": round(reserved_today, 4),
        "today_calls": int(day.get("calls", 0)),
        "month_usd": round(month_spent, 4),
        "daily_usd": caps["daily_usd"],
        "monthly_usd": caps["monthly_usd"],
        "daily_calls": caps["daily_calls"],
        "disabled_until": state["disabled_until"] if disabled else 0.0,
        "disabled_reason": state["disabled_reason"] if disabled else "",
        "cooldown_seconds": max(0, int(state["cooldown_until"] - now)),
        "daily_exhausted": (
            spent_today + reserved_today >= caps["daily_usd"] or int(day.get("calls", 0)) >= caps["daily_calls"]
        ),
        "monthly_exhausted": month_spent + reserved_today >= caps["monthly_usd"],
    }
