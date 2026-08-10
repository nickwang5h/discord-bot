import asyncio
import datetime
import json
import logging
import re

import discord
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, TZ
from core import settings, ai_client, news_cache, data_ingester
from core.jobs import run_delivery_job
from core.utils import create_ai_embed

logger = logging.getLogger(__name__)

ANALYSIS_BATCH_SIZE = 8
ANALYSIS_MAX_OUTPUT_TOKENS = 3000
SCORE_FIELDS = (
    "relevance_score",
    "novelty_score",
    "quality_score",
    "llm_interestingness",
    "cross_domain_bridge",
)


def _normalize_scored_items(
    scored_items: list[object],
    source_items: list[dict],
) -> list[dict]:
    """Validate model output and restore source-owned fields before caching."""
    sources_by_url = {item.get("url"): item for item in source_items if item.get("url")}
    normalized: list[dict] = []

    for candidate in scored_items:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url")
        source = sources_by_url.get(url)
        if source is None:
            continue

        try:
            scores = {
                field: min(1.0, max(0.0, float(candidate[field])))
                for field in SCORE_FIELDS
            }
        except (KeyError, TypeError, ValueError):
            continue

        summary = str(candidate.get("summary") or "").strip()
        topic = str(candidate.get("topic") or "").strip()
        connection_reason = str(candidate.get("connection_reason") or "").strip()
        if not summary or not topic or not connection_reason:
            continue

        normalized_item = {
            "title": source.get("title") or str(candidate.get("title") or "").strip(),
            "url": url,
            "source": source.get("source"),
            "summary": summary,
            "topic": topic,
            "connection_reason": connection_reason,
            **scores,
        }
        normalized_item["discovery_score"] = (
            (0.25 * scores["relevance_score"])
            + (0.20 * scores["novelty_score"])
            + (0.20 * scores["quality_score"])
            + (0.20 * scores["llm_interestingness"])
            + (0.15 * scores["cross_domain_bridge"])
        )
        normalized.append(normalized_item)

    return normalized


class AdvancedNews(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._fetch_lock = asyncio.Lock()
        self._digest_delivery_lock = asyncio.Lock()
        self._skip_initial_hourly_fetch = True
        removed = news_cache.prune_legacy_items()
        if removed:
            logger.info("[Advanced News] 已清理 %s 条旧版或无效缓存记录", removed)
        if SCHEDULED_JOBS_ENABLED:
            self.hourly_fetch.start()
            self.scheduled_digest.start()
        else:
            logger.info("[Advanced News] 自动抓取和定时精读已通过部署配置禁用")

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
        if self._fetch_lock.locked():
            logger.info("[Advanced News] 已有抓取任务运行，本次触发跳过")
            return

        async with self._fetch_lock:
            await self._process_hourly_fetch_locked()

    async def _process_hourly_fetch_locked(self):
        logger.info("[Advanced News] 开始每小时的数据抓取")
        raw_items = await data_ingester.fetch_all_sources()
        
        # Filter out ones already in cache before sending to AI to save tokens
        new_items = news_cache.filter_new_items(raw_items)
                
        if not new_items:
            logger.info("[Advanced News] 没有发现新的资讯")
            return
            
        logger.info("[Advanced News] 发现 %s 条新资讯，正在交由 AI 分析", len(new_items))
        
        # Small batches keep prompt + completion below free-provider TPM limits.
        for i in range(0, len(new_items), ANALYSIS_BATCH_SIZE):
            batch = new_items[i:i + ANALYSIS_BATCH_SIZE]
            
            prompt_text = "以下是新抓取的资讯：\n\n"
            for idx, item in enumerate(batch):
                prompt_text += f"[{idx+1}] 标题: {item['title']}\n来源分类: {item['source']}\n链接: {item['url']}\n内容摘要: {item['content'][:300]}...\n\n"
                
            system_prompt = (
                "你是一个高级的新闻分析和过滤引擎。请对提供的资讯列表进行去重和深度打分评估。\n"
                "你必须严格返回一个 JSON 对象，包含一个 `news` 数组，不要包含任何其他文字解释。\n"
                "【严格的质量与新奇度过滤规则】：\n"
                "1. 区分“随机小众”和“可连接的新奇”。只给那些能与用户兴趣建立连接的新奇内容打高分。\n"
                "2. 寻找反直觉结论、跨领域迁移、技术变化、文化信号或独特案例。\n"
                "3. 拒绝低质量、标题党、纯营销、无可验证内容。模型不能仅凭来源名判断质量，应基于标题、摘要、正文证据和来源历史指标。\n"
                "【字段说明】（所有分数 0.0 - 1.0，可以使用小数）：\n"
                "- `title`: 原标题\n"
                "- `url`: 原链接\n"
                "- `summary`: 1-2 句话的深度硬核摘要\n"
                "- `topic`: 一个极简的主题标签（如 'AI模型', '地缘政治', '芯片' 等，用于后续去重）\n"
                "- `relevance_score`: 与『科技/AI』和『金融』兴趣集群的最近相关度（0.0-1.0）\n"
                "- `novelty_score`: 相比于常见的日常新闻，它的新颖度（0.0-1.0）\n"
                "- `quality_score`: 来源与内容的综合质量（0.0-1.0）\n"
                "- `llm_interestingness`: 作为大模型，你觉得它有多有趣/值得一读（0.0-1.0）\n"
                "- `cross_domain_bridge`: 是否提供了跨领域的启发（0.0-1.0）\n"
                "- `connection_reason`: 必须指出和用户已有兴趣的具体连接，绝对不允许使用“你可能感兴趣”这种废话，必须具体（如“这揭示了AI在材料科学的新应用”）。\n"
                "示例输出格式：\n"
                "{\n  \"news\": [\n    {\"title\": \"标题\", \"url\": \"http...\", \"summary\": \"摘要\", \"topic\": \"AI硬件\", \"relevance_score\": 0.9, \"novelty_score\": 0.8, \"quality_score\": 0.9, \"llm_interestingness\": 0.8, \"cross_domain_bridge\": 0.5, \"connection_reason\": \"具体连接理由\"}\n  ]\n}\n"
            )
            
            response = None
            clean_resp = None
            try:
                result = await ai_client.generate_ai(
                    prompt_text,
                    system=system_prompt,
                    use_search=False,
                    json_mode=True,
                    max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
                )
                response = result.text
                clean_resp = self._clean_json_response(response)
                parsed = json.loads(clean_resp)
                scored_items = parsed.get("news", parsed) if isinstance(parsed, dict) else parsed

                if not isinstance(scored_items, list):
                    raise ValueError("模型 JSON 中的 news 必须是数组")
                scored_items = _normalize_scored_items(scored_items, batch)

                # Append to cache
                added = news_cache.add_items(scored_items)
                logger.info("[Advanced News] 批次处理完成，成功存入 %s 条记录", added)
            except ai_client.AIServiceUnavailable as error:
                logger.error(
                    "[Advanced News] AI 节点全部不可用，停止剩余 %s 个批次: %s",
                    (len(new_items) - i + ANALYSIS_BATCH_SIZE - 1) // ANALYSIS_BATCH_SIZE,
                    error,
                )
                break
            except Exception as e:
                logger.exception("[Advanced News] AI 分析或 JSON 解析失败: %s", e)
                if response:
                    logger.debug("[Advanced News] 原始模型输出片段: %s", response[:800])
                if clean_resp:
                    logger.debug("[Advanced News] JSON 文本片段: %s", clean_resp[:800])

    async def _build_scheduled_digest(self, time_name):
        logger.info("[Advanced News] 正在生成 %s 精读简报", time_name)
        
        unpushed = news_cache.get_unpushed_items()
        if not unpushed:
            logger.info("[Advanced News] 缓存中没有未推送的新闻，跳过")
            return
            
        # Apply hard constraints
        valid_items = []
        for item in unpushed:
            try:
                qual = float(item.get("quality_score", 0.0))
                nov = float(item.get("novelty_score", 0.0))
                rel = float(item.get("relevance_score", 0.0))
                if qual >= 0.60 and nov >= 0.40 and rel >= 0.30:
                    valid_items.append(item)
            except (ValueError, TypeError):
                pass
                
        # Sort by discovery_score descending
        valid_items.sort(key=lambda x: x.get("discovery_score", 0.0), reverse=True)
        
        # Greedily select max 10 items, max 2 per topic
        selected_items = []
        topic_counts = {}
        for item in valid_items:
            topic = item.get("topic", "未分类")
            if topic_counts.get(topic, 0) < 2:
                selected_items.append(item)
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
            if len(selected_items) >= 10:
                break
        
        if not selected_items:
            return
            
        prompt_text = "以下是为您精选的高分资讯（包含科技/金融主线，以及高新奇度的拓展阅读）：\n\n"
        for item in selected_items:
            prompt_text += f"- 标题: {item.get('title')}\n  链接: {item.get('url')}\n  推荐理由: {item.get('connection_reason')}\n  预摘要: {item.get('summary')}\n\n"
            
        system_prompt = (
            f"你是一个高级私人主编。用户为你提供了一批已经经过初步打分筛选的高质量资讯。\n"
            f"请你负责将它们整理成一篇排版精美、逻辑连贯的 Discord Markdown 简报。\n"
            f"注意：这是每天生成的【{time_name}特刊】，请在导语中带上今天的时间感，提炼出这半天以来的核心脉络。\n"
            f"要求：\n"
            f"1. 将内容分为两大板块：【🚀 核心焦点 (Tech & Finance)】和【🔮 灵感与视野 (打破茧房的拓展发现)】。\n"
            f"2. 排版规范：**必须加粗原新闻标题**，并且**摘要内容必须保持正常的非加粗文本**。\n"
            f"3. 极简主义：每条新闻的摘要必须极其简短（严格控制在核心的一两句话内，绝不啰嗦）。\n"
            f"4. 正确的单条格式范例：\n"
            f"   - **[新闻的原始标题](URL)**：这里是一句直击要害的极简摘要。\n"
            f"5. 在最开头写一句简短的主编导语，一语道破这批资讯的核心脉络。\n"
            f"6. 每条新闻必须以 `- ` 开头。绝对禁止使用 Markdown 或 HTML 表格。\n"
            f"7. 只输出一份完整简报，禁止重复板块、重复标题或在后面重写第二版。\n"
        )
        
        digest = await ai_client.ask_ai(
            prompt_text,
            system=system_prompt,
            use_search=False,
            raise_on_failure=True,
        )
        
        embed = create_ai_embed(
            title=f"💎 高级精读简报 ({time_name}特刊)",
            description=digest,
            color=discord.Color.purple()
        )
        
        pushed_urls = [item.get("url") for item in selected_items]
        return embed, pushed_urls

    async def _run_scheduled_digest(self, channel, time_name):
        async def deliver(result):
            embed, _pushed_urls = result
            return await channel.send(embed=embed)

        def mark_delivered(result):
            _embed, pushed_urls = result
            news_cache.mark_as_pushed(pushed_urls)
            news_cache.clear_pushed()

        return await run_delivery_job(
            lock=self._digest_delivery_lock,
            task_name=f"高级精读简报生成 ({time_name})",
            build=lambda: self._build_scheduled_digest(time_name),
            deliver=deliver,
            on_delivered=mark_delivered,
        )

    @tasks.loop(minutes=60)
    async def hourly_fetch(self):
        if self._skip_initial_hourly_fetch:
            self._skip_initial_hourly_fetch = False
            logger.info("[Advanced News] 跳过启动时即时补抓，首次自动抓取将在一小时后执行")
            return
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
            logger.warning("[Advanced News] 未设置 TEST_NEWS_CHANNEL_ID，跳过推送")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("[Advanced News] 找不到配置的频道 ID: %s", channel_id)
            return
            
        try:
            await self._run_scheduled_digest(channel, time_name)
        except Exception as e:
            logger.exception("高级精读定时任务失败: %s", e)

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
        asyncio.create_task(self._run_scheduled_digest(channel, "测试"))

async def setup(bot):
    await bot.add_cog(AdvancedNews(bot))
