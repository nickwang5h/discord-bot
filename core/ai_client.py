import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if api_key and api_key != "your_gemini_api_key_here":
    client = genai.Client(api_key=api_key)
    model_available = True
else:
    client = None
    model_available = False
    print("WARNING: GEMINI_API_KEY 未配置，AI 功能将不可用。")

async def summarize(text: str, system: str = "用简洁中文总结要点，分条列出。"):
    if not model_available or not client:
        return "AI 未配置，无法总结。"
    
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=system
            )
        )
        return response.text
    except Exception as e:
        return f"AI 生成失败: {e}"
