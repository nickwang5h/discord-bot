import asyncio
import os
import re
import time
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv(override=True)

from core import settings

client = None
model_available = False
gemini_cooldown_until = 0.0


class AIServiceUnavailable(RuntimeError):
    """所有可用模型节点都失败时抛出的可重试异常。"""


def reload_client() -> bool:
    global client, model_available
    # 优先从 settings.json 读取，如果没有再尝试从环境变量读取
    api_key = settings.get_setting("GEMINI_API_KEY")
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")
        
    if api_key and api_key != "your_gemini_api_key_here":
        try:
            client = genai.Client(api_key=api_key)
            model_available = True
            return True
        except Exception as e:
            print(f"初始化 Gemini Client 失败: {e}")
            client = None
            model_available = False
            return False
    else:
        client = None
        model_available = False
        print("WARNING: GEMINI_API_KEY 未配置，将尝试使用已配置的备用 AI 服务。")
        return False

# 启动时初始化
reload_client()

async def _ask_groq(text: str, sys_prompt: str, json_mode: bool = False):
    import aiohttp
    api_key = settings.get_setting("GROQ_API_KEY") or os.getenv("GROQ_API_KEY")
    if not api_key:
        raise Exception("未配置 GROQ_API_KEY")
        
    models_to_try = [
        "qwen/qwen3.6-27b",
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile"
    ]
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": text})
    
    last_error = ""
    async with aiohttp.ClientSession() as session:
        for model_name in models_to_try:
            payload = {
                "model": model_name,
                "messages": messages
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            try:
                async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            content = data["choices"][0]["message"]["content"]
                            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
                            content = re.sub(r'<think>.*', '', content, flags=re.DOTALL).strip()
                            if not content:
                                raise Exception("模型思考被打断或超时，未能输出有效内容")
                            return f"{content}\n\n<!--MODEL:Groq ({model_name})-->"
                        except (KeyError, IndexError):
                            raise Exception(f"解析返回格式失败: {data}")
                    else:
                        error_text = await resp.text()
                        last_error = f"HTTP {resp.status}: {error_text}"
                        print(f"[Groq Fallback] 节点 {model_name} 失败: {last_error}")
                        continue
            except Exception as e:
                last_error = str(e)
                print(f"[Groq Fallback] 请求 {model_name} 抛出异常: {last_error}")
                continue
                
    raise Exception(f"所有 Groq 备选节点均已耗尽。最后一次错误: {last_error}")

async def _ask_zhipu(text: str, sys_prompt: str, json_mode: bool = False):
    import aiohttp
    api_key = settings.get_setting("ZHIPU_API_KEY") or os.getenv("ZHIPU_API_KEY")
    if not api_key:
        raise Exception("未配置 ZHIPU_API_KEY")
        
    model_name = "glm-4.7-flash"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": text})
    
    payload = {
        "model": model_name,
        "messages": messages
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://open.bigmodel.cn/api/paas/v4/chat/completions", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    try:
                        content = data["choices"][0]["message"]["content"]
                        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
                        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL).strip()
                        if not content:
                            raise Exception("模型思考被打断或超时，未能输出有效内容")
                        return f"{content}\n\n<!--MODEL:Zhipu ({model_name})-->"
                    except (KeyError, IndexError):
                        raise Exception(f"解析返回格式失败: {data}")
                else:
                    error_text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {error_text}")
        except Exception as e:
            print(f"[Zhipu Fallback] 请求 {model_name} 抛出异常: {e}")
            raise

async def _ask_openrouter(text: str, sys_prompt: str, json_mode: bool = False):
    import aiohttp
    api_key = settings.get_setting("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise Exception("未配置 OPENROUTER_API_KEY")
        
    # 定义备选模型瀑布流 (用户自定义的排在第一位，后面跟着系统推荐的顶级免费节点)
    user_model = settings.get_setting("OPENROUTER_MODEL") or os.getenv("OPENROUTER_MODEL")
    fallback_models = [
        # 以下 ID 于 2026-07-22 通过 OpenRouter /api/v1/models 实时确认。
        # 前三个支持 response_format，优先用于高级新闻的 JSON 输出。
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ]
    
    models_to_try = []
    if user_model:
        models_to_try.append(user_model)
    for m in fallback_models:
        if m not in models_to_try:
            models_to_try.append(m)

    if json_mode:
        json_incompatible_models = {"nvidia/nemotron-3-ultra-550b-a55b:free"}
        models_to_try = [m for m in models_to_try if m not in json_incompatible_models]
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": text})
    
    last_error = ""
    
    async with aiohttp.ClientSession() as session:
        for model_name in models_to_try:
            payload = {
                "model": model_name,
                "messages": messages
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            try:
                async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            content = data["choices"][0]["message"]["content"]
                            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
                            content = re.sub(r'<think>.*', '', content, flags=re.DOTALL).strip()
                            if not content:
                                raise Exception("模型思考被打断或超时，未能输出有效内容")
                            return f"{content}\n\n<!--MODEL:OpenRouter ({model_name})-->"
                        except (KeyError, IndexError):
                            raise Exception(f"解析返回格式失败: {data}")
                    else:
                        error_text = await resp.text()
                        last_error = f"HTTP {resp.status}: {error_text}"
                        print(f"[OpenRouter Fallback] 节点 {model_name} 失败: {last_error}")
                        continue # 尝试下一个节点
            except Exception as e:
                last_error = str(e)
                print(f"[OpenRouter Fallback] 请求 {model_name} 抛出异常: {last_error}")
                continue
                
    raise Exception(f"所有 OpenRouter 备选节点均已耗尽。最后一次错误: {last_error}")

async def ask_ai(
    text: str,
    system: str = "用简洁中文总结要点，分条列出。",
    use_search: bool = False,
    fallback_offline: bool = True,
    json_mode: bool = False,
    raise_on_failure: bool = False,
):
    global gemini_cooldown_until

    # 优先从 settings 中读取，如果没设置则退化使用环境变量或默认值
    model_name = settings.get_setting("GEMINI_MODEL") or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    
    async def _try_gemini(with_search: bool):
        tools = []
        if with_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
            
        config_kwargs = {}
        if system:
            config_kwargs["system_instruction"] = system
        if tools:
            config_kwargs["tools"] = tools
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
            
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=text,
                    config=config
                ),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            raise Exception("Gemini API 请求超时 (30s)")
        content = (response.text or "").strip()
        if not content:
            raise Exception("Gemini 返回了空响应")
        return f"{content}\n\n<!--MODEL:Gemini ({model_name})-->"

    def _enable_gemini_cooldown(error: Exception) -> bool:
        """记录限流冷却时间；返回本次错误是否为限流错误。"""
        global gemini_cooldown_until
        error_msg = str(error)
        if "429" not in error_msg and "RESOURCE_EXHAUSTED" not in error_msg:
            return False

        delay = 60.0
        match = re.search(r"retry(?: in|Delay['\": ]+)?\s*([\d.]+)s", error_msg, re.IGNORECASE)
        if match:
            delay = max(1.0, float(match.group(1)))
        gemini_cooldown_until = max(gemini_cooldown_until, time.time() + delay)
        print(f"[Cooldown Enabled] Gemini 触发 429，未来 {delay} 秒内的请求将直接走兜底。")
        return True

    gemini_error = "Gemini 未配置"
    gemini_ready = model_available and client is not None
    in_cooldown = time.time() < gemini_cooldown_until

    if gemini_ready and not in_cooldown:
        try:
            # Tier 1: Gemini（按调用方要求决定是否启用 Search）
            return await _try_gemini(with_search=use_search)
        except Exception as error:
            gemini_error = str(error)
            print(f"[Fallback Triggered] Gemini API 抛出异常: {gemini_error}")
            rate_limited = _enable_gemini_cooldown(error)

            if use_search:
                if not fallback_offline:
                    return "⚠️ **联网功能暂不可用**：Gemini 联网请求失败，且当前设置禁止离线降级。"

                # 限流时不能立刻重试同一服务，否则会绕过刚设置的 cooldown。
                if not rate_limited:
                    try:
                        return await _try_gemini(with_search=False)
                    except Exception as offline_error:
                        gemini_error = str(offline_error)
                        _enable_gemini_cooldown(offline_error)
                        print(f"[Fallback Triggered] Gemini 离线调用也失败: {gemini_error}")
                else:
                    print("[Fallback Triggered] Gemini 正在限流，跳过同服务的离线重试。")
    else:
        if in_cooldown:
            remaining = max(1, int(gemini_cooldown_until - time.time()))
            gemini_error = f"Gemini 冷却中（剩余约 {remaining} 秒）"
            print(f"[Fallback Triggered] {gemini_error}，直接使用备用服务。")
        else:
            print("[Fallback Triggered] Gemini 未配置，直接使用备用服务。")

        if use_search and not fallback_offline:
            return "⚠️ **联网功能暂不可用**：Gemini 当前不可用，且当前设置禁止离线降级。"

    # Tier 2.2: Groq 极速节点
    try:
        return await _ask_groq(text, system, json_mode=json_mode)
    except Exception as groq_err:
        print(f"[Fallback Triggered] Groq 兜底失败: {groq_err}")

    # Tier 2.5: Zhipu (GLM-4.7-Flash)
    try:
        return await _ask_zhipu(text, system, json_mode=json_mode)
    except Exception as zhipu_err:
        print(f"[Fallback Triggered] 智谱 AI 兜底失败: {zhipu_err}")

    # Tier 3: OpenRouter 终极兜底
    try:
        return await _ask_openrouter(text, system, json_mode=json_mode)
    except Exception as or_err:
        print(f"[Fallback Exhausted] Gemini: {gemini_error}; OpenRouter: {or_err}")
        message = "⚠️ **AI 服务暂时不可用**：所有已配置的模型节点均请求失败，请稍后重试。"
        if raise_on_failure:
            raise AIServiceUnavailable(message) from or_err
        return message
