import asyncio
import re
from dataclasses import dataclass

import aiohttp


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    supports_json: bool = True
    reasoning_effort: str | None = None
    reasoning_format: str | None = None


@dataclass(frozen=True, slots=True)
class AIResult:
    text: str
    provider: str
    model: str

    @property
    def attribution(self) -> str:
        return f"{self.provider} ({self.model})"

    def as_legacy_text(self) -> str:
        return f"{self.text}\n\n<!--MODEL:{self.attribution}-->"


def clean_model_content(content: object) -> str:
    if not isinstance(content, str):
        raise ProviderError("模型返回的 content 不是字符串")
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    cleaned = re.sub(r"<think>.*", "", cleaned, flags=re.DOTALL).strip()
    if not cleaned:
        raise ProviderError("模型未返回有效内容")
    return cleaned


async def request_openai_compatible(
    *,
    provider: str,
    endpoint: str,
    api_key: str,
    models: list[ModelSpec],
    text: str,
    system: str,
    json_mode: bool,
    timeout_seconds: float,
    max_output_tokens: int,
    token_limit_field: str = "max_tokens",
    extra_payload: dict[str, object] | None = None,
) -> AIResult:
    """Call an OpenAI-compatible endpoint with ordered model failover."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": text})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    failures: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    async with aiohttp.ClientSession() as session:
        for spec in models:
            if json_mode and not spec.supports_json:
                continue

            remaining = deadline - loop.time()
            if remaining <= 0:
                failures.append(f"provider 总超时 ({timeout_seconds:g}s)")
                break

            payload: dict[str, object] = {
                "model": spec.model_id,
                "messages": messages,
                token_limit_field: max_output_tokens,
            }
            if extra_payload:
                payload.update(extra_payload)
            if spec.reasoning_effort is not None:
                payload["reasoning_effort"] = spec.reasoning_effort
            if spec.reasoning_format is not None:
                payload["reasoning_format"] = spec.reasoning_format
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                request_timeout = aiohttp.ClientTimeout(total=remaining)
                async with session.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                ) as response:
                    if response.status != 200:
                        body = (await response.text())[:300].replace("\n", " ")
                        failures.append(f"{spec.model_id}: HTTP {response.status} {body}")
                        if response.status == 413:
                            break
                        continue

                    data = await response.json()
                    try:
                        choice = data["choices"][0]
                        content = choice["message"]["content"]
                    except (KeyError, IndexError, TypeError) as error:
                        failures.append(f"{spec.model_id}: 返回结构无效 ({error})")
                        continue

                    if choice.get("finish_reason") == "length":
                        failures.append(f"{spec.model_id}: 输出达到 token 上限，结果不完整")
                        continue

                    try:
                        cleaned = clean_model_content(content)
                    except ProviderError as error:
                        failures.append(f"{spec.model_id}: {error}")
                        continue
                    return AIResult(cleaned, provider, spec.model_id)
            except (aiohttp.ClientError, TimeoutError) as error:
                failures.append(f"{spec.model_id}: {type(error).__name__}: {error}")

    detail = "; ".join(failures[-3:]) if failures else "没有兼容当前输出模式的模型"
    raise ProviderError(f"{provider} 所有候选模型失败：{detail}")
