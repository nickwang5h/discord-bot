import discord
import re
import asyncio
from typing import Callable, Any, Coroutine

def create_ai_embed(title: str, description: str, color: discord.Color = discord.Color.blue()) -> discord.Embed:
    """
    创建一个统一格式的 AI 回复 Embed 卡片，并自动处理 Discord 4096 字符限制截断。
    """
    footer_text = "✨ Powered by AI"
    
    fallback_indicator = "> 💡 *(由于主网络限流，本条回复已自动切换至 OpenRouter 免费节点生成)*"
    if fallback_indicator in description:
        description = description.replace(f"\n\n{fallback_indicator}", "")
        description = description.replace(fallback_indicator, "")
        
    model_match = re.search(r'<!--MODEL:(.*?)-->', description)
    if model_match:
        model_name = model_match.group(1)
        footer_text = f"✨ Powered by {model_name}"
        description = re.sub(r'\n*<!--MODEL:.*?-->\n*', '', description)
        
    if len(description) > 4000:
        description = description[:4000] + "\n\n...(内容过长，已被自动截断)"
        
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    embed.set_footer(text=footer_text)
    return embed

async def with_retry(task_name: str, coro_func: Callable[[], Coroutine[Any, Any, Any]], max_retries: int = 5, delay: int = 300) -> Any:
    """
    通用的重试包装函数，适用于需要定期执行且易受网络波动的任务。
    如果在最大重试次数内失败，则会引发最终异常并记录错误日志。
    """
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[{task_name}] 执行失败 (尝试 {attempt+1}/{max_retries}): {e}，将在 {delay} 秒后重试...")
                await asyncio.sleep(delay)
            else:
                print(f"🚨 紧急：[{task_name}] 在重试 {max_retries} 次后彻底告负: {e}")
                # 此处可以选择通知管理员 (如通过 webhook)
                raise
