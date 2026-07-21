import datetime
import zoneinfo
import json
import re
import asyncio
from discord.ext import commands, tasks
import discord

from core import settings, ai_client, news_cache, data_ingester
from core.utils import create_ai_embed, with_retry

TZ = zoneinfo.ZoneInfo("America/Toronto")

class AdvancedNews(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hourly_fetch.start()
        self.scheduled_digest.start()

    def cog_unload(self):
        self.hourly_fetch.cancel()
        self.scheduled_digest.cancel()
        
    def _clean_json_response(self, text: str) -> str:
        """Helper to extract JSON from possible markdown formatting."""
        # Strip DeepSeek reasoning blocks if any
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        
        match = re.search(r'```(?:json)?(.*?)```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        
        # Fallback: locate the JSON array or object boundaries
        text = text.strip()
        if text.startswith('[') and text.endswith(']'):
            return text
        if text.startswith('{') and text.endswith('}'):
            return text
            
        start_array = text.find('[')
        end_array = text.rfind(']')
        start_obj = text.find('{')
        end_obj = text.rfind('}')
        
        is_array = start_array != -1 and end_array != -1 and start_array < end_array
        is_obj = start_obj != -1 and end_obj != -1 and start_obj < end_obj
        
        if is_array and is_obj:
            if start_array < start_obj:
                return text[start_array:end_array+1]
            else:
                return text[start_obj:end_obj+1]
        elif is_array:
            return text[start_array:end_array+1]
        elif is_obj:
            return text[start_obj:end_obj+1]
            
        return text

    async def _process_hourly_fetch(self):
        print("[Advanced News] 开始每小时的数据抓取...")
        raw_items = await data_ingester.fetch_all_sources()
        
        # Filter out ones already in cache before sending to AI to save tokens
        new_items = []
        for item in raw_items:
            if not news_cache.is_duplicate(item["url"], item["title"]):
                new_items.append(item)
                
        if not new_items:
            print("[Advanced News] 没有发现新的资讯。")
            return
            
        print(f"[Advanced News] 发现 {len(new_items)} 条新资讯，正在交由 AI 分析...")
        
        # We process in batches to avoid context limit if there are too many
        batch_size = 15
        for i in range(0, len(new_items), batch_size):
            batch = new_items[i:i+batch_size]
            
            prompt_text = "以下是新抓取的资讯：\n\n"
            for idx, item in enumerate(batch):
                prompt_text += f"[{idx+1}] 标题: {item['title']}\n来源分类: {item['source']}\n链接: {item['url']}\n内容摘要: {item['content'][:300]}...\n\n"
                
            system_prompt = (
                "你是一个高级的新闻分析和过滤引擎。请对提供的资讯列表进行去重和打分评估。\n"
                "你必须严格返回一个 JSON 对象，包含一个 `news` 数组，不要包含任何其他文字解释。\n"
                "每个数组元素的字段如下：\n"
                "- `title`: 新闻原标题\n"
                "- `url`: 新闻原链接\n"
                "- `summary`: 1-2 句话的深度硬核摘要\n"
                "- `theme_score`: 1-10分，基于与『科技 (Tech/AI)』和『金融 (Finance)』主题的相关度和重要性打分。\n"
                "- `serendipity_score`: 1-10分，这是『打破信息茧房』的分数。如果这条新闻虽然不是科技或金融，但非常新奇、有趣、或者蕴含极高价值的洞察，请给高分。\n"
                "示例输出格式：\n"
                "{\n  \"news\": [\n    {\"title\": \"标题\", \"url\": \"http...\", \"summary\": \"摘要\", \"theme_score\": 8, \"serendipity_score\": 5}\n  ]\n}\n"
                "注意：如果发现两条资讯讲述完全相同的事情，请只保留其中更全面的一条。"
            )
            
            response = None
            clean_resp = None
            try:
                response = await ai_client.ask_ai(prompt_text, system=system_prompt, use_search=False, json_mode=True)
                clean_resp = self._clean_json_response(response)
                parsed = json.loads(clean_resp)
                scored_items = parsed.get("news", parsed) if isinstance(parsed, dict) else parsed

                
                # Append to cache
                added = news_cache.add_items(scored_items)
                print(f"[Advanced News] 批次处理完成，成功存入 {added} 条记录。")
            except Exception as e:
                print(f"[Advanced News] AI 分析或 JSON 解析失败: {e}")
                if response:
                    print(f"[Advanced News] 导致失败的原始模型输出片段: {response[:800]}")
                if clean_resp:
                    print(f"[Advanced News] 提取后尝试解析的文本片段: {clean_resp[:800]}")

    async def _process_scheduled_digest(self, channel, time_name):
        print(f"[Advanced News] 正在生成 {time_name} 精读简报...")
        
        unpushed = news_cache.get_unpushed_items()
        if not unpushed:
            print("[Advanced News] 缓存中没有未推送的新闻，跳过。")
            return
            
        # Select top items: Top 5 by theme_score, Top 3 by serendipity_score
        unpushed.sort(key=lambda x: x.get("theme_score", 0), reverse=True)
        top_theme = unpushed[:7]
        
        remaining = [item for item in unpushed if item not in top_theme]
        remaining.sort(key=lambda x: x.get("serendipity_score", 0), reverse=True)
        top_serendipity = remaining[:3]
        
        selected_items = top_theme + top_serendipity
        
        if not selected_items:
            return
            
        prompt_text = "以下是为您精选的高分资讯（包含科技/金融主线，以及高新奇度的拓展阅读）：\n\n"
        for item in selected_items:
            prompt_text += f"- 标题: {item.get('title')}\n  链接: {item.get('url')}\n  主题分: {item.get('theme_score')}, 新奇分: {item.get('serendipity_score')}\n  预摘要: {item.get('summary')}\n\n"
            
        system_prompt = (
            "你是一个高级私人主编。用户为你提供了一批已经经过初步打分筛选的高质量资讯。\n"
            "请你负责将它们整理成一篇排版精美、逻辑连贯的 Discord Markdown 简报。\n"
            "注意：这是每天生成的【早间/晚间特刊】，请在导语中带上今天的时间感，提炼出这半天以来的核心脉络。\n"
            "要求：\n"
            "1. 将内容分为两大板块：【🚀 核心焦点 (Tech & Finance)】和【🔮 灵感与视野 (打破茧房的拓展发现)】。\n"
            "2. 使用给定的预摘要，但你可以润色使其更引人入胜。必须包含原文的 Markdown 链接 `[标题](URL)`。\n"
            "3. 在开头写一句简短的主编导语，总结这批资讯的核心脉络。\n"
        )
        
        digest = await ai_client.ask_ai(prompt_text, system=system_prompt, use_search=False)
        
        embed = create_ai_embed(
            title=f"💎 高级精读简报 ({time_name}特刊)",
            description=digest,
            color=discord.Color.purple()
        )
        
        await channel.send(embed=embed)
        
        # Mark as pushed and clear
        pushed_urls = [item.get("url") for item in selected_items]
        news_cache.mark_as_pushed(pushed_urls)
        news_cache.clear_pushed()

    @tasks.loop(minutes=60)
    async def hourly_fetch(self):
        await self._process_hourly_fetch()

    @hourly_fetch.before_loop
    async def before_hourly_fetch(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=[
        datetime.time(hour=8, minute=0, tzinfo=TZ),
        datetime.time(hour=18, minute=0, tzinfo=TZ)
    ])
    async def scheduled_digest(self):
        now = datetime.datetime.now(tz=TZ)
        is_morning = now.hour < 12
        time_name = "早间" if is_morning else "晚间"
        
        channel_id = settings.get_setting("TEST_NEWS_CHANNEL_ID")
        if not channel_id:
            print("[Advanced News] 未设置 TEST_NEWS_CHANNEL_ID，跳过推送。")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            print(f"[Advanced News] 找不到配置的频道 ID: {channel_id}")
            return
            
        try:
            await with_retry(f"高级精读简报 ({time_name})", lambda: self._process_scheduled_digest(channel, time_name))
        except Exception as e:
            print(f"Scheduled digest failed: {e}")

    @scheduled_digest.before_loop
    async def before_scheduled_digest(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="test_hourly_fetch", description="[实验] 手动触发一次高级资讯抓取与打分")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_hourly_fetch_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在后台执行抓取和打分，请查看控制台日志...", ephemeral=True)
        # 异步执行，不阻塞 Discord
        asyncio.create_task(self._process_hourly_fetch())

    @discord.app_commands.command(name="test_scheduled_digest", description="[实验] 手动触发一次高级精读简报推送")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_scheduled_digest_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在生成精读简报，请稍等...", ephemeral=True)
        channel = interaction.channel
        asyncio.create_task(self._process_scheduled_digest(channel, "测试"))

async def setup(bot):
    await bot.add_cog(AdvancedNews(bot))
