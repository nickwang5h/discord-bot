import asyncio
import logging
import re
import time

from google import genai
from google.genai import types

from config import get_env
from core import settings
from core.ai_providers import AIResult, ModelSpec, ProviderError, request_openai_compatible

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
GROQ_MODELS = [
    ModelSpec(
        "qwen/qwen3.6-27b",
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
    ModelSpec("openai/gpt-oss-20b:free"),
    ModelSpec("nvidia/nemotron-nano-9b-v2:free"),
]

client: genai.Client | None = None
model_available = False
gemini_cooldown_until = 0.0


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


async def generate_ai(
    text: str,
    system: str = "用简洁中文总结要点，分条列出。",
    use_search: bool = False,
    fallback_offline: bool = True,
    json_mode: bool = False,
    max_output_tokens: int = 4096,
) -> AIResult:
    """Route basic generation to Qwen first and reserve Gemini priority for Search."""
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
    }


reload_client()
