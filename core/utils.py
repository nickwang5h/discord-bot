import discord
import re

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
