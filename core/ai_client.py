import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import anthropic
from google import genai
from google.genai import types

from config import get_env
from core import claude_budget, settings
from core.ai_providers import AIResult, ModelSpec, ProviderError, request_openai_compatible

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
GROQ_MODELS = [
    ModelSpec(
        "qwen/qwen3.8-27b",
        reasoning_effort="none",
        reasoning_format="hidden",
    ),
    ModelSpec("openai/gpt-oss-120b", reasoning_effort="low"),
    ModelSpec("openai/gpt-oss-20b", reasoning_effort="low"),
]
ZHIPU_MODELS = [
    ModelSpec("glm-4.7-flash"),
    ModelSpec("glm-4.5-flash"),
]
OPENROUTER_MODELS = [
    ModelSpec("nvidia/nemotron-3-super-120b-a12b:free"),
    ModelSpec("nvidia/nemotron-3-ultra-550b-a55b:free", supports_json=False),
    ModelSpec("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"),
]

CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_TIMEOUT_SECONDS = 90.0
CLAUDE_DEFAULT_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ClaudeRoute:
    effort: str
    max_tokens: int
    max_input_chars: int
    daily_calls: int


# Only these personal routes may use Claude; shared topics never pass a route.
CLAUDE_ROUTES: dict[str, ClaudeRoute] = {
    "personal.following": ClaudeRoute("medium", 6000, 30000, 4),
    "watch.confirm": ClaudeRoute("low", 3000, 12000, 6),
    "watch.aliases": ClaudeRoute("low", 1000, 1000, 3),
    "recall.plan": ClaudeRoute("low", 1000, 1500, 15),
    "recall.answer": ClaudeRoute("medium", 4000, 10000, 15),
    "review.summary": ClaudeRoute("low", 1500, 8000, 1),
}

client: genai.Client | None = None
model_available = False
gemini_cooldown_until = 0.0
claude_registered = False
_claude_client: anthropic.AsyncAnthropic | None = None


class AIServiceUnavailable(RuntimeError):
    """Raised when every configured model provider fails."""


def reload_client() -> bool:
    global client, model_available
    api_key = settings.get_secret("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key_here":
        client = None
        model_available = False
        logger.warning("GEMINI_API_KEY 未配置，将使用已配置的备用 AI 服务")
        return False

    try:
        client = genai.Client(api_key=api_key)
        model_available = True
        return True
    except Exception as error:
        client = None
        model_available = False
        logger.exception("初始化 Gemini Client 失败: %s", error)
        return False


def reload_claude_client() -> bool:
    """Register Claude only when ANTHROPIC_API_KEY is present; never log the key."""
    global claude_registered, _claude_client
    _claude_client = None
    claude_registered = bool(settings.get_secret("ANTHROPIC_API_KEY"))
    if not claude_registered:
        logger.info("ANTHROPIC_API_KEY 未配置，个人路由使用免费 provider 链")
    return claude_registered


def _new_claude_client(api_key: str) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        max_retries=0,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )


def _get_claude_client() -> anthropic.AsyncAnthropic | None:
    global _claude_client
    if not claude_registered:
        return None
    if _claude_client is None:
        api_key = settings.get_secret("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        _claude_client = _new_claude_client(api_key)
    return _claude_client


def _gemini_model() -> str:
    return settings.get_setting("GEMINI_MODEL") or get_env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)


def _record_gemini_cooldown(error: Exception) -> bool:
    global gemini_cooldown_until
    message = str(error)
    if "429" not in message and "RESOURCE_EXHAUSTED" not in message:
        return False

    delay = 60.0
    match = re.search(r"retry(?: in|Delay['\": ]+)?\s*([\d.]+)s", message, re.IGNORECASE)
    if match:
        delay = max(1.0, float(match.group(1)))
    gemini_cooldown_until = max(gemini_cooldown_until, time.time() + delay)
    logger.warning("Gemini 触发限流，未来 %.1f 秒直接使用备用服务", delay)
    return True


async def _ask_gemini(
    text: str,
    system: str,
    *,
    with_search: bool,
    json_mode: bool,
    max_output_tokens: int,
) -> AIResult:
    if client is None:
        raise ProviderError("Gemini 未初始化")

    tools = [types.Tool(google_search=types.GoogleSearch())] if with_search else []
    config_kwargs: dict[str, object] = {}
    if system:
        config_kwargs["system_instruction"] = system
    if tools:
        config_kwargs["tools"] = tools
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    config_kwargs["max_output_tokens"] = max_output_tokens
    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_gemini_model(),
                contents=text,
                config=config,
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError as error:
        raise ProviderError("Gemini API 请求超时 (30s)") from error

    content = (response.text or "").strip()
    if not content:
        raise ProviderError("Gemini 返回了空响应")
    return AIResult(content, "Gemini", _gemini_model())


async def _ask_groq(
    text: str,
    system: str,
    json_mode: bool = False,
    max_output_tokens: int = 4096,
) -> AIResult:
    api_key = settings.get_secret("GROQ_API_KEY")
    if not api_key:
        raise ProviderError("未配置 GROQ_API_KEY")
    return await request_openai_compatible(
        provider="Groq",
        endpoint="https://api.groq.com/openai/v1/chat/completions",
        api_key=api_key,
        models=GROQ_MODELS,
        text=text,
        system=system,
        json_mode=json_mode,
        timeout_seconds=20,
        max_output_tokens=max_output_tokens,
        token_limit_field="max_completion_tokens",
    )


async def _ask_zhipu(
    text: str,
    system: str,
    json_mode: bool = False,
    max_output_tokens: int = 4096,
) -> AIResult:
    api_key = settings.get_secret("ZHIPU_API_KEY")
    if not api_key:
        raise ProviderError("未配置 ZHIPU_API_KEY")
    return await request_openai_compatible(
        provider="Zhipu",
        endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        api_key=api_key,
        models=ZHIPU_MODELS,
        text=text,
        system=system,
        json_mode=json_mode,
        timeout_seconds=25,
        max_output_tokens=max_output_tokens,
        extra_payload={"thinking": {"type": "disabled"}},
    )


async def _ask_openrouter(
    text: str,
    system: str,
    json_mode: bool = False,
    max_output_tokens: int = 4096,
) -> AIResult:
    api_key = settings.get_secret("OPENROUTER_API_KEY")
    if not api_key:
        raise ProviderError("未配置 OPENROUTER_API_KEY")

    models = list(OPENROUTER_MODELS)
    user_model = settings.get_setting("OPENROUTER_MODEL") or get_env("OPENROUTER_MODEL")
    if user_model and all(spec.model_id != user_model for spec in models):
        models.insert(0, ModelSpec(user_model))

    return await request_openai_compatible(
        provider="OpenRouter",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        api_key=api_key,
        models=models,
        text=text,
        system=system,
        json_mode=json_mode,
        timeout_seconds=25,
        max_output_tokens=max_output_tokens,
    )


async def _ask_compatible_providers(
    text: str,
    system: str,
    *,
    json_mode: bool,
    max_output_tokens: int,
    errors: list[str],
) -> AIResult | None:
    providers = (
        ("Groq", _ask_groq),
        ("Zhipu", _ask_zhipu),
        ("OpenRouter", _ask_openrouter),
    )
    for provider_name, provider in providers:
        try:
            return await provider(
                text,
                system,
                json_mode=json_mode,
                max_output_tokens=max_output_tokens,
            )
        except Exception as error:
            errors.append(f"{provider_name}: {error}")
            logger.warning("%s 请求失败: %s", provider_name, error)
    return None


class _ClaudeFailure(Exception):
    """A Claude attempt that should fall through to the free provider chain."""


def _retry_after_seconds(error: anthropic.APIStatusError) -> float:
    raw = error.response.headers.get("retry-after")
    try:
        delay = float(raw) if raw is not None else CLAUDE_DEFAULT_COOLDOWN_SECONDS
    except ValueError:
        delay = CLAUDE_DEFAULT_COOLDOWN_SECONDS
    if delay != delay or delay <= 0:
        delay = CLAUDE_DEFAULT_COOLDOWN_SECONDS
    return min(delay, 3600.0)


def _is_credit_exhausted(error: anthropic.APIStatusError) -> bool:
    if error.status_code == 402 or error.type == "billing_error":
        return True
    return isinstance(error, anthropic.BadRequestError) and "credit balance is too low" in str(error).lower()


def _record_claude_error(error: BaseException) -> None:
    """Classify a failed Claude request (design §1.5) and update availability state."""
    now = time.time()
    name = type(error).__name__
    if isinstance(error, anthropic.APIStatusError) and _is_credit_exhausted(error):
        claude_budget.disable_until(claude_budget.next_utc_midnight(now), "额度耗尽")
        logger.warning("Claude 额度耗尽（HTTP %s），今日剩余时间改用免费链", error.status_code)
    elif isinstance(error, anthropic.RateLimitError):
        delay = _retry_after_seconds(error)
        claude_budget.cooldown_until(now + delay)
        logger.warning("Claude 触发限流，冷却 %.0f 秒", delay)
    elif isinstance(
        error,
        (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError),
    ):
        claude_budget.disable_until(claude_budget.next_utc_midnight(now), f"HTTP {error.status_code}")
        logger.error("Claude 认证/权限/模型不可用（%s, HTTP %s），停用至下一个 UTC 日", name, error.status_code)
    elif isinstance(error, anthropic.BadRequestError):
        logger.error("Claude 请求被拒绝（HTTP 400）：%s", str(error)[:300])
    elif isinstance(error, anthropic.InternalServerError) or (
        isinstance(error, anthropic.APIStatusError) and error.status_code >= 500
    ):
        claude_budget.cooldown_until(now + CLAUDE_DEFAULT_COOLDOWN_SECONDS)
        logger.warning("Claude 服务端错误（HTTP %s），冷却 %.0f 秒", error.status_code, CLAUDE_DEFAULT_COOLDOWN_SECONDS)
    elif isinstance(error, anthropic.APITimeoutError) or isinstance(error, TimeoutError):
        logger.warning("Claude 请求超时 (%.0fs)", CLAUDE_TIMEOUT_SECONDS)
    elif isinstance(error, anthropic.APIConnectionError):
        logger.warning("Claude 网络错误：%s", name)
    elif isinstance(error, anthropic.APIStatusError):
        logger.warning("Claude 请求失败（HTTP %s）", error.status_code)
    else:
        logger.warning("Claude 请求失败：%s", name)


def _usage_tokens(response: object) -> tuple[int, int]:
    usage = getattr(response, "usage", None)

    def number(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and value > 0 else 0

    input_tokens = (
        number("input_tokens") + number("cache_creation_input_tokens") + number("cache_read_input_tokens")
    )
    return input_tokens, number("output_tokens")


async def _ask_claude(
    text: str,
    system: str,
    *,
    route: str,
    json_schema: dict | None,
) -> AIResult | None:
    """Try Claude for a personal route. Returns None to continue on the free chain."""
    spec = CLAUDE_ROUTES.get(route)
    if spec is None:
        logger.debug("route %s 不在 CLAUDE_ROUTES 中，不使用 Claude", route)
        return None
    claude = _get_claude_client()
    if claude is None:
        return None
    if len(text) > spec.max_input_chars:
        logger.warning("Claude route %s 输入 %d 字符超过上限 %d，改用免费链", route, len(text), spec.max_input_chars)
        return None
    blocked = claude_budget.blocked_reason()
    if blocked is not None:
        logger.info("Claude %s，route %s 改用免费链", blocked, route)
        return None

    worst = claude_budget.worst_case_usd(
        claude_budget.estimate_input_tokens(system, text),
        spec.max_tokens,
    )
    reservation = claude_budget.reserve(route, worst, spec.daily_calls)
    if reservation is None:
        logger.info("Claude 预算或 route %s 次数已满，改用免费链", route)
        return None

    output_config: dict[str, object] = {"effort": spec.effort}
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    request: dict[str, object] = {
        "model": CLAUDE_MODEL,
        "max_tokens": spec.max_tokens,
        "messages": [{"role": "user", "content": text}],
        "output_config": output_config,
    }
    if system:
        request["system"] = system

    input_tokens = output_tokens = 0
    try:
        try:
            async with asyncio.timeout(CLAUDE_TIMEOUT_SECONDS):
                response = await claude.messages.create(**request)
        except Exception as error:
            _record_claude_error(error)
            return None

        input_tokens, output_tokens = _usage_tokens(response)
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason in ("refusal", "max_tokens"):
            logger.warning("Claude route %s 以 %s 结束，改用免费链", route, stop_reason)
            return None
        if stop_reason not in ("end_turn", "stop_sequence"):
            logger.warning("Claude route %s 返回未预期的 stop_reason=%s，改用免费链", route, stop_reason)
            return None

        content = "".join(
            block.text
            for block in getattr(response, "content", None) or []
            if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
        ).strip()
        if not content:
            logger.warning("Claude route %s 返回空正文，改用免费链", route)
            return None
        if json_schema is not None:
            try:
                json.loads(content)
            except ValueError:
                logger.warning("Claude route %s 返回的 JSON 无效，改用免费链", route)
                return None
        return AIResult(content, "Claude", CLAUDE_MODEL)
    finally:
        claude_budget.settle(reservation, input_tokens, output_tokens)


async def generate_ai(
    text: str,
    system: str = "用简洁中文总结要点，分条列出。",
    use_search: bool = False,
    fallback_offline: bool = True,
    json_mode: bool = False,
    max_output_tokens: int = 4096,
    route: str | None = None,
    json_schema: dict | None = None,
) -> AIResult:
    """Route basic generation to Qwen first and reserve Gemini priority for Search.

    A known personal ``route`` (see ``CLAUDE_ROUTES``) without search tries Claude first
    and falls through to the unchanged chain below on any Claude failure.
    """
    if route is not None and not use_search and (json_schema is not None or not json_mode):
        claude_result = await _ask_claude(text, system, route=route, json_schema=json_schema)
        if claude_result is not None:
            return claude_result

    errors: list[str] = []
    in_cooldown = time.time() < gemini_cooldown_until

    if not use_search:
        compatible_result = await _ask_compatible_providers(
            text,
            system,
            json_mode=json_mode,
            max_output_tokens=max_output_tokens,
            errors=errors,
        )
        if compatible_result is not None:
            return compatible_result

    if model_available and client is not None and not in_cooldown:
        try:
            return await _ask_gemini(
                text,
                system,
                with_search=use_search,
                json_mode=json_mode,
                max_output_tokens=max_output_tokens,
            )
        except Exception as error:
            errors.append(f"Gemini: {error}")
            logger.warning("Gemini 请求失败: %s", error)
            rate_limited = _record_gemini_cooldown(error)

            if use_search and not fallback_offline:
                raise AIServiceUnavailable("Gemini 联网请求失败，且禁止离线降级") from error
            if use_search and not rate_limited:
                try:
                    return await _ask_gemini(
                        text,
                        system,
                        with_search=False,
                        json_mode=json_mode,
                        max_output_tokens=max_output_tokens,
                    )
                except Exception as offline_error:
                    errors.append(f"Gemini offline: {offline_error}")
                    _record_gemini_cooldown(offline_error)
                    logger.warning("Gemini 离线请求失败: %s", offline_error)
    else:
        reason = "冷却中" if in_cooldown else "未配置"
        errors.append(f"Gemini: {reason}")
        if use_search and not fallback_offline:
            raise AIServiceUnavailable(f"Gemini {reason}，且禁止离线降级")

    if use_search:
        compatible_result = await _ask_compatible_providers(
            text,
            system,
            json_mode=json_mode,
            max_output_tokens=max_output_tokens,
            errors=errors,
        )
        if compatible_result is not None:
            return compatible_result

    logger.error("AI 服务全部失败: %s", " | ".join(errors))
    raise AIServiceUnavailable("所有已配置的模型节点均请求失败")


async def ask_ai(
    text: str,
    system: str = "用简洁中文总结要点，分条列出。",
    use_search: bool = False,
    fallback_offline: bool = True,
    json_mode: bool = False,
    raise_on_failure: bool = False,
    max_output_tokens: int = 4096,
) -> str:
    """Backward-compatible string API used by existing Cogs and scripts."""
    try:
        result = await generate_ai(
            text,
            system=system,
            use_search=use_search,
            fallback_offline=fallback_offline,
            json_mode=json_mode,
            max_output_tokens=max_output_tokens,
        )
        return result.as_legacy_text()
    except AIServiceUnavailable:
        if raise_on_failure:
            raise
        if use_search and not fallback_offline:
            return "⚠️ **联网功能暂不可用**：Gemini 联网服务当前不可用。"
        return "⚠️ **AI 服务暂时不可用**：所有已配置的模型节点均请求失败，请稍后重试。"


def get_provider_status() -> dict[str, object]:
    """Return non-sensitive status information for health checks and admin commands."""
    return {
        "gemini": bool(settings.get_secret("GEMINI_API_KEY")),
        "groq": bool(settings.get_secret("GROQ_API_KEY")),
        "zhipu": bool(settings.get_secret("ZHIPU_API_KEY")),
        "openrouter": bool(settings.get_secret("OPENROUTER_API_KEY")),
        "gemini_model": _gemini_model(),
        "gemini_cooldown_seconds": max(0, int(gemini_cooldown_until - time.time())),
        "claude": claude_registered,
        "claude_model": CLAUDE_MODEL,
        "claude_usage": claude_budget.status(),
    }


reload_client()
reload_claude_client()
