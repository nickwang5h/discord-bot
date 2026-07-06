import discord
from discord.ext import commands
from discord import app_commands
from core import ai_client
from core.utils import create_ai_embed

class DevTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="explain", description="[Dev] 极客词典：用大白话解释一个技术概念")
    async def explain(self, interaction: discord.Interaction, concept: str, context: str = ""):
        await interaction.response.defer()
        prompt = f"请向我解释这个技术概念：【{concept}】。补充上下文：{context}。"
        system = (
            "你是一个资深的程序员导师。请用最接地气的大白话、结合生活中的生动比喻来解释这个概念。"
            "如果可能，提供一段极简的伪代码或示例代码帮助理解。"
            "直接输出干货，不要在开头打招呼，也不要追问。"
        )
        answer = await ai_client.ask_ai(prompt, system=system)
        embed = create_ai_embed(
            title=f"📖 概念解析: {concept}",
            description=answer,
            color=discord.Color.purple()
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="vs", description="[Dev] 技术对比：一针见血对比两个技术栈")
    async def vs(self, interaction: discord.Interaction, tech_a: str, tech_b: str):
        await interaction.response.defer()
        prompt = f"对比这两个技术：{tech_a} vs {tech_b}。"
        system = (
            "你是一个客观的高级架构师。请直接列出这两个技术的核心差异、优缺点、最适合的业务场景。"
            "最后给出一个明确的“极客推荐结论”，帮助团队做技术选型。结构化输出，禁止废话和寒暄。"
        )
        answer = await ai_client.ask_ai(prompt, system=system)
        embed = create_ai_embed(
            title=f"⚔️ 技术对比: {tech_a} vs {tech_b}",
            description=answer,
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="regex", description="[Dev] 正则生成器：根据描述生成正则表达式并拆解解释")
    async def regex(self, interaction: discord.Interaction, description: str):
        await interaction.response.defer()
        prompt = f"我需要一个正则表达式来实现：{description}。"
        system = (
            "你是一个正则表达式大师。首先用 Markdown 代码块输出正确的正则表达式。"
            "然后，像切菜一样，逐字逐句拆解这个表达式，向开发者解释每个符号的含义。"
            "最后给出一两个匹配成功的例子和失败的例子。禁止任何废话。"
        )
        answer = await ai_client.ask_ai(prompt, system=system)
        embed = create_ai_embed(
            title=f"🪄 正则生成: {description}",
            description=answer,
            color=discord.Color.teal()
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="debug", description="[Dev] 报错翻译官：分析错误日志或代码")
    async def debug(self, interaction: discord.Interaction, code_or_error: str):
        await interaction.response.defer()
        prompt = f"请帮我排查这段代码或报错信息：\n{code_or_error}"
        system = (
            "你是一个排错专家（Debug Master）。分析用户提供的代码或错误日志，指出根本原因。"
            "用清晰的步骤解释如何修复，如果可以，请直接提供修复后的代码片段。语气专业直接。"
        )
        answer = await ai_client.ask_ai(prompt, system=system)
        embed = create_ai_embed(
            title="🐛 Debug 分析结果",
            description=answer,
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(DevTools(bot))
