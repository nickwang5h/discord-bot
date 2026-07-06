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

async def ask_ai(text: str, system: str = "用简洁中文总结要点，分条列出。", use_search: bool = False):
    if not model_available or not client:
        return "⚠️ 当前尚未配置大模型 API Key，请联系管理员使用 `/set_gemini_key` 进行配置。"
    
    # 优先从 settings 中读取，如果没设置则退化使用环境变量或默认值
    model_name = settings.get_setting("GEMINI_MODEL")
    if not model_name:
        model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        
    tools = []
    if use_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
        
    config_kwargs = {}
    if system:
        config_kwargs["system_instruction"] = system
    if tools:
        config_kwargs["tools"] = tools
        
    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        
    try:
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=text,
            config=config
        )
        return response.text
    except Exception as e:
        error_msg = str(e)
        if "429 RESOURCE_EXHAUSTED" in error_msg:
            return "⚠️ **AI 服务调用已达上限 (Rate Limit)**。\n免费版 API 每天仅允许几十次请求。请联系管理员前往 Google AI Studio 绑定信用卡（Set up billing）以解锁高频调用限制。"
        return f"AI 生成失败: {e}"
