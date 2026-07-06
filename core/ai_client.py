import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

from core import settings

client = None
model_available = False

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
        print("WARNING: GEMINI_API_KEY 未配置，AI 功能将不可用，请使用 /set_gemini_key 进行配置。")
        return False

# 启动时初始化
reload_client()

async def _ask_openrouter(text: str, sys_prompt: str):
    import aiohttp
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise Exception("未配置 OPENROUTER_API_KEY")
        
    model_name = settings.get_setting("OPENROUTER_MODEL") or os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-exp:free")
    
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
    
    async with aiohttp.ClientSession() as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"OpenRouter API Error {resp.status}: {error_text}")
            
            data = await resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
                return f"{content}\n\n> 💡 *(由于主网络限流，本条回复已自动切换至 OpenRouter 免费节点生成)*\n\n<!--MODEL:OpenRouter ({model_name})-->"
            except (KeyError, IndexError):
                raise Exception(f"解析 OpenRouter 返回格式失败: {data}")

async def ask_ai(text: str, system: str = "用简洁中文总结要点，分条列出。", use_search: bool = False, fallback_offline: bool = True):
    if not model_available or not client:
        return "⚠️ 当前尚未配置大模型 API Key，请联系管理员使用 `/set_gemini_key` 进行配置。"
    
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
            
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=text,
            config=config
        )
        return f"{response.text}\n\n<!--MODEL:Gemini ({model_name})-->"
        
    try:
        # Tier 1: Gemini with Search
        return await _try_gemini(with_search=use_search)
    except Exception as e:
        error_msg = str(e)
        if "429 RESOURCE_EXHAUSTED" in error_msg or "Rate Limit" in error_msg:
            # 搜索模式触发了限流，进入降级策略
            if use_search:
                if not fallback_offline:
                    return "⚠️ **联网额度告急**：由于 Gemini Search 额度耗尽，无法执行在线搜索，且当前设置禁止离线降级。"
                # Tier 2: Gemini without Search (Offline)
                try:
                    return await _try_gemini(with_search=False)
                except Exception as offline_e:
                    offline_err_msg = str(offline_e)
                    if not ("429" in offline_err_msg or "Rate Limit" in offline_err_msg):
                        return f"AI 离线生成失败: {offline_e}"
            
            # Tier 3: OpenRouter
            try:
                openrouter_resp = await _ask_openrouter(text, system)
                return openrouter_resp
            except Exception as or_err:
                return "⚠️ **AI 服务全线告急**。\n主干 Gemini 额度耗尽，且后备 OpenRouter 节点唤醒失败。请联系管理员检查后台日志。"
        
        return f"AI 生成失败: {e}"
