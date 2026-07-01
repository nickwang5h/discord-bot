import discord

def create_ai_embed(title: str, description: str, color: discord.Color = discord.Color.blue()) -> discord.Embed:
    """
    创建一个统一格式的 AI 回复 Embed 卡片，并自动处理 Discord 4096 字符限制截断。
    """
    if len(description) > 4000:
        description = description[:4000] + "\n\n...(内容过长，已被自动截断)"
        
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    embed.set_footer(text="✨ Powered by Gemini AI")
    return embed
