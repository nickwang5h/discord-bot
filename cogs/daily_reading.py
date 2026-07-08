import datetime
import zoneinfo
from discord.ext import commands, tasks
import discord
import asyncio
from core import settings, ai_client
from core.utils import create_ai_embed
import random

TZ = zoneinfo.ZoneInfo("America/Toronto")

class DailyReading(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reading_loop.start()

    def cog_unload(self):
        self.reading_loop.cancel()

    @tasks.loop(time=[datetime.time(hour=7, minute=30, tzinfo=TZ)])
    async def reading_loop(self):
        print("执行每日英文阅读推送任务...")
        channel_id = settings.get_setting("READING_CHANNEL_ID")
        if not channel_id:
            print("未设置 READING_CHANNEL_ID，跳过每日英文阅读推送。")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            print(f"找不到配置的频道 ID: {channel_id}")
            return

        try:
            tasks_to_run = [
                ("🗣️ 每日英语：实用场景", discord.Color.blue(), self.generate_scenario),
                ("📰 每日英语：外刊精读", discord.Color.green(), self.generate_rss_reading),
                ("🎙️ 每日英语：TED 演讲精选", discord.Color.purple(), self.generate_ted_reading)
            ]

            for i, (title, color, func) in enumerate(tasks_to_run):
                try:
                    result = await func()
                    if result:
                        embed = create_ai_embed(
                            title=title,
                            description=result,
                            color=color
                        )
                        message = await channel.send(embed=embed)
                        # 添加打卡 reaction
                        await message.add_reaction("✅")
                except Exception as e:
                    print(f"生成阅读卡片 {title} 失败: {e}")
                
                # 如果不是最后一个任务，等待 60 秒再执行下一个，避免并发请求 AI 导致速率限制或内容过长
                if i < len(tasks_to_run) - 1:
                    await asyncio.sleep(60)
                    
        except Exception as e:
            print(f"执行阅读推送失败: {e}")

    async def generate_scenario(self):
        system_prompt = (
            "你是一个专业的英语老师。请生成一段简短的职场或生活实用英语对话/短文，长度约 150 词。\n"
            "要求：\n"
            "1. 每天选择一个不同的随机场景（例如：星巴克点单、委婉拒绝会议、请求延期、茶水间闲聊等）。首先用一句中文说明今天的场景。\n"
            "2. 提供纯英文的正文内容，难度控制在雅思 6.0 (CEFR B2) 左右，表达地道。\n"
            "3. 在文末提取 3-5 个核心实用词汇或短语，提供中文解释。\n"
            "4. **绝对禁止**使用 Markdown 表格。请使用简单的加粗列表（如 `- **单词**: 解释`）来展示词汇。\n"
            "5. 严格使用 Markdown 格式，不要加多余的寒暄语。"
        )
        return await ai_client.ask_ai("请生成今天的实用场景英语阅读素材。", system=system_prompt, use_search=False)

    async def generate_rss_reading(self):
        try:
            import feedparser
            # 这里选取 Lifehacker 或者 NPR
            urls = [
                "https://feeds.npr.org/1004/rss.xml", # NPR World
                "https://feeds.npr.org/1048/rss.xml", # NPR Science
                "https://feeds.npr.org/1046/rss.xml"  # NPR Pop Culture
            ]
            url = random.choice(urls)
            feed = await asyncio.to_thread(feedparser.parse, url)
            
            if not feed.entries:
                return None
                
            entry = feed.entries[0]
            raw_text = f"Title: {entry.title}\nLink: {entry.link}\nSummary: {entry.summary if hasattr(entry, 'summary') else ''}"
            
            system_prompt = (
                "你是一个专业的英语外刊精读老师。\n"
                "我给你提供了一篇真实外媒新闻的标题和摘要。由于摘要较短，请你按以下结构生成阅读材料：\n"
                "1. 在开头附上新闻标题和链接。\n"
                "2. 【原貌呈现】：将原摘要润色整理为一小段纯正的英文（作为核心事实，**严禁捏造任何原新闻没有提到的事实、数据或引用**）。\n"
                "3. 【深度短评】：围绕该新闻话题，以客观观察者的视角写一段约 80-100 词的英文短评或背景探讨（Insight / Commentary）。这是为了扩充阅读量，但请明确这是对该话题的延伸探讨，避免与原新闻事实混淆。\n"
                "4. 提供一段优美的中文大意总结（涵盖新闻事实与短评）。\n"
                "5. 提取 3 个左右核心好词/词组并作中文解释。\n"
                "6. **绝对禁止**使用 Markdown 表格。请使用简单的加粗列表（如 `- **单词**: 解释`）来展示词汇。\n"
                "7. 严格使用 Markdown 格式，排版美观。"
            )
            return await ai_client.ask_ai(raw_text, system=system_prompt, use_search=False)
        except Exception as e:
            print(f"抓取或生成 RSS 阅读失败: {e}")
            return None

    async def generate_ted_reading(self):
        try:
            import feedparser
            import random
            # 使用 TED 官方 RSS 源
            url = "https://www.ted.com/talks/rss"
            feed = await asyncio.to_thread(feedparser.parse, url)
            
            if not feed.entries:
                return None
                
            # 随机选择前 20 个最新演讲中的一个，保持新鲜感
            entry = random.choice(feed.entries[:20])
            raw_text = f"Title: {entry.title}\nLink: {entry.link}\nSummary: {entry.summary if hasattr(entry, 'summary') else ''}"
            
            system_prompt = (
                "你是一个充满智慧的英语外教。\n"
                "我为你提供了一篇最新 TED 演讲的标题和摘要。由于仅有摘要信息，请按以下结构生成阅读卡片：\n"
                "1. 在开头附上演讲标题和真实的原始链接。\n"
                "2. 【演讲简介】：将提供的摘要整理为一小段地道的英文介绍（Overview）。**严禁凭空捏造演讲者没有说过的话或强加观点**。\n"
                "3. 【延伸反思】：围绕该演讲的核心主题，以读者的视角写一段约 100-150 词的深度英文反思（Reflection / Insight）。这一段旨在提供高质量的阅读语料，请围绕话题进行充满启发性的独立探讨。\n"
                "4. 提供一段优美的中文大意总结（涵盖简介与反思）。\n"
                "5. 提取 3-5 个核心好词/词组，并作中文解释。\n"
                "6. **绝对禁止**使用 Markdown 表格。请使用简单的加粗列表（如 `- **单词**: 解释`）来展示词汇。\n"
                "7. 严格使用 Markdown 格式，排版美观。"
            )
            return await ai_client.ask_ai(raw_text, system=system_prompt, use_search=False)
        except Exception as e:
            print(f"抓取或生成 TED 阅读失败: {e}")
            return None

    @reading_loop.before_loop
    async def before_reading_loop(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="test_reading", description="[管理员] 立即测试每日英文阅读推送")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_reading(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在为您生成每日阅读材料，请稍等...", ephemeral=True)
        await self.reading_loop.coro(self)

async def setup(bot):
    await bot.add_cog(DailyReading(bot))
