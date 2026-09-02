import asyncio
import datetime
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, STATE_ROOT, TZ
from core import ai_client, settings
from core.feeds import FeedSource, fetch_feed
from core.jobs import RetryPolicy, retry_async
from core.storage import JsonStore
from core.utils import create_ai_embed

logger = logging.getLogger(__name__)

SCENARIO_POOL: list[dict[str, str]] = [
    # 🏢 职场与技术交流 (Workplace & Tech)
    {"id": "daily_standup", "name": "Daily Standup 敏捷站会", "context": "向团队简明扼要汇报昨日完成的任务、今日计划以及当前遇到的 Blocker。"},
    {"id": "code_review", "name": "友好提出 Code Review 建议", "context": "在 PR 中向同事指出一处潜在的并发性能问题，语气谦虚专业并附上具体的重构建议。"},
    {"id": "salary_negotiation", "name": "薪资谈判与 Offer 沟通", "context": "收到 HR 的 Offer 邮件后，礼貌感谢并基于个人经验与市场水准提出期望薪资区间。"},
    {"id": "incident_postmortem", "name": "线上服务故障复盘 (Postmortem)", "context": "向团队解释线上故障原因、影响范围以及后续预防重蹈覆辙的 Action Items。"},
    {"id": "pushback_deadline", "name": "委婉推迟不合理的项目交付期", "context": "向 Product Manager 阐述技术难点与不可抗力风险，提出将交付时间顺延一周。"},
    {"id": "one_on_one", "name": "与主管的 1-on-1 职业目标沟通", "context": "向主管探讨在下一个季度承担更多系统架构设计责任的想法与成长规划。"},
    {"id": "slack_async_comms", "name": "跨时区异步 Slack 沟通", "context": "向远端同事清晰描述遇到的技术上下文，提供复现步骤，避免低效反复确认。"},
    {"id": "team_lunch_icebreak", "name": "新员工入职午餐破冰闲聊", "context": "在午餐桌上向新同事介绍自己的背景，自然聊起周末爱好与本地美食。"},
    {"id": "leave_of_absence", "name": "年假申请与工作交接邮件", "context": "向上级申请带薪年假 (PTO)，并清晰交代紧急联系方式与各主要项目的代班同事。"},
    {"id": "pitch_new_tool", "name": "向技术团队推荐并引入开源工具", "context": "阐述引入该工具能解决当前的何种性能痛点，并分析迁移收益与成本。"},
    {"id": "client_bug_escalation", "name": "向重要客户安抚并解释生产 Bug", "context": "在客户支持工单中礼貌致歉，说明正在紧急修复，并承诺更新进度的确切时间点。"},
    {"id": "cross_team_collaboration", "name": "跨部门协作争取设计资源", "context": "向 UI/UX 设计团队负责人沟通下一个产品迭代的设计资源排期与优先级。"},

    # 🍁 加拿大本地生活与政务 (Canadian Daily Life)
    {"id": "service_ontario", "name": "ServiceOntario 办理驾照换发", "context": "向柜台工作人员出示旧驾照与地址证明，咨询 G牌更新手续与新卡邮寄周期。"},
    {"id": "walk_in_clinic", "name": "Walk-in 诊所向医生描述症状", "context": "向医生准确描述持续三天的干咳、低烧与胸闷，询问是否需要开处方抗生素。"},
    {"id": "winter_tire_booking", "name": "预约汽车更换雪胎 (Winter Tires)", "context": "打电话给汽车修配厂预约换胎时间，询问动平衡与旧四季胎代存费用。"},
    {"id": "bank_wire_transfer", "name": "银行柜台办理大额境外汇款", "context": "向银行柜员提供 SWIFT Code 与收款人信息，确认到账周期与每日限额。"},
    {"id": "supermarket_refund", "name": "超市/Costco 办理退换货", "context": "带上小票向客户服务台说明包装破损，礼貌要求退款并退回原支付卡。"},
    {"id": "apartment_lease_heating", "name": "向房东发邮件反映暖气故障", "context": "在寒冬向房东陈述室内温度低于法定最低温度，礼貌要求尽快派水暖工检修。"},
    {"id": "apartment_viewing", "name": "租房看房了解水电气网细节", "context": "看房时向中介询问水电气网 (Utilities) 是否全包、车位费用以及租约条款。"},
    {"id": "cbsa_airport_customs", "name": "机场入境海关申报 (CBSA)", "context": "向边境官员回答入境目的、居住地址以及是否携带需要申报的免税额度外物品。"},
    {"id": "veterinary_clinic", "name": "带宠物去兽医诊所打疫苗与体检", "context": "向兽医说明宠物的食欲与排便情况，预约接种狂犬疫苗与年度驱虫。"},
    {"id": "snow_removal_neighbor", "name": "暴雪后与邻居商量除雪与车道清理", "context": "暴风雪过后与隔壁邻居友好沟通清扫车道交界处的积雪，避免雪堆挡住出车视线。"},
    {"id": "home_internet_installation", "name": "联系宽带客服排查光纤断网", "context": "向运营商客服说明光猫光信号红灯闪烁，已尝试重启，申请安排技术人员上门检测。"},
    {"id": "community_center_sports", "name": "社区活动中心预约羽毛球场", "context": "向前台咨询周末室内羽毛球场的空闲时段与 Drop-in 费用，并完成预约。"},

    # ☕ 社交与休闲出行 (Social & Dining)
    {"id": "coffee_specialty", "name": "精品咖啡馆点单与风味咨询", "context": "向咖啡师询问今日浅烘手冲豆的花果香气特征，点一杯燕麦奶 Flat White。"},
    {"id": "restaurant_split_bill", "name": "西餐厅聚餐买单与分摊账单", "context": "向服务生礼貌示意买单，说明大家需要分单 (Separate checks) 并分别刷卡支付。"},
    {"id": "farmers_market", "name": "本地农贸市场挑选新鲜当季食材", "context": "向农场摊主询问苹果与纯枫糖浆的产地与保质期，挑选无农药种植的蔬菜。"},
    {"id": "gym_membership", "name": "健身房咨询会员协议与私教课", "context": "向前台了解按月扣款协议、是否有器械指导课程，以及取消合同的违约条款。"},
    {"id": "weekend_hiking_plan", "name": "商量周末去国家公园徒步露营", "context": "与朋友讨论往返拼车 (Carpool)、携带防熊喷雾与登山杖等行前安全准备。"},
    {"id": "bookstore_recommendation", "name": "在独立书店请店员推荐小说", "context": "说明自己最近喜欢科幻悬疑类小说，请店员推荐一本节奏紧凑的畅销佳作。"},
    {"id": "small_talk_weather", "name": "电梯或公交站的经典 Small Talk", "context": "与偶遇的邻居自然而然地聊起今天罕见的暖冬阳光与融雪天气。"},

    # 🎓 学习与进阶效率 (Study & Career)
    {"id": "academic_office_hour", "name": "大学 Professor 的 Office Hour 请教", "context": "向教授请教论文中某个算法复杂度的推导难点，并寻求参考论文推荐。"},
    {"id": "library_study_room", "name": "图书馆借用安静自习室与设施", "context": "向图书管理员出示学生卡/读者证，预订带白板的讨论室两小时。"},
    {"id": "tech_meetup_networking", "name": "本地 Tech Meetup 寻找开源合作者", "context": "听完演讲后与讲者交流，讨论如何在当前项目中集成类似架构。"},
    {"id": "presentation_qna", "name": "技术分享会后的 Q&A 互动", "context": "礼貌感谢提问者的好问题，清晰解答该方案在边缘高并发情况下的取舍。"},
]

_history_store = JsonStore(STATE_ROOT / "data/reading_history.json", list)


def _pick_distinct_scenario() -> dict[str, str]:
    """从场景库中挑选场景，保证最近 12 次执行内绝不重复。"""
    try:
        history = _history_store.read()
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    recent_ids = set(history[-12:]) if history else set()
    available = [s for s in SCENARIO_POOL if s["id"] not in recent_ids]
    if not available:
        available = SCENARIO_POOL

    chosen = random.choice(available)
    history.append(chosen["id"])
    if len(history) > 40:
        history = history[-40:]

    try:
        _history_store.write(history)
    except Exception as error:
        logger.warning("保存阅读历史记录失败: %s", error)

    return chosen


class DailyReading(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delivery_lock = asyncio.Lock()
        if SCHEDULED_JOBS_ENABLED:
            self.reading_loop.start()
        else:
            logger.info("每日英文阅读定时任务已通过部署配置禁用")

    def cog_unload(self):
        self.reading_loop.cancel()

    @tasks.loop(time=[datetime.time(hour=7, minute=30, tzinfo=TZ)])
    async def reading_loop(self):
        logger.info("执行每日英文阅读推送任务")
        channel_id = settings.get_setting("READING_CHANNEL_ID")
        if not channel_id:
            logger.warning("未设置 READING_CHANNEL_ID，跳过每日英文阅读推送")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("找不到配置的频道 ID: %s", channel_id)
            return

        await self._run_reading(channel)

    async def _run_reading(self, channel: discord.abc.Messageable):
        if self._delivery_lock.locked():
            logger.warning("每日英文阅读已有任务执行中，跳过重复触发")
            return

        async with self._delivery_lock:
            tasks_to_run = [
                ("🗣️ 每日英语：实用场景", discord.Color.blue(), self.generate_scenario),
                ("📰 每日英语：外刊精读", discord.Color.green(), self.generate_rss_reading),
                ("🎙️ 每日英语：TED 演讲精选", discord.Color.purple(), self.generate_ted_reading),
            ]
            retry_policy = RetryPolicy(attempts=2, initial_delay_seconds=30)

            for index, (title, color, generate) in enumerate(tasks_to_run):
                try:
                    result = await retry_async(title, generate, policy=retry_policy)
                    if result:
                        embed = create_ai_embed(title=title, description=result, color=color)
                        message = await channel.send(embed=embed)
                        try:
                            await message.add_reaction("✅")
                        except discord.HTTPException:
                            logger.warning("阅读卡片已发送，但添加打卡 reaction 失败: %s", title)
                except Exception as error:
                    logger.exception("生成阅读卡片 %s 失败: %s", title, error)

                if index < len(tasks_to_run) - 1:
                    await asyncio.sleep(60)

    async def generate_scenario(self) -> str:
        scenario = _pick_distinct_scenario()
        system_prompt = (
            "你是一个专业的英语私教老师。\n"
            "用户为你指定了今日的具体情境与沟通目标。请围绕该情境，编写一段地道、自然、生动的纯英文对话或短文（长度约 150-180 词）。\n"
            "要求：\n"
            "1. 首先用一句简明的中文概述今天的场景背景与核心沟通目标。\n"
            "2. 正文使用纯正英文，难度控制在雅思 6.0 ~ 6.5 (CEFR B2) 左右，用词地道自然，符合北美真实沟通习惯，杜绝生硬的中式翻译。\n"
            "3. 在文末提取 3-5 个该情境下最核心的地道词汇、职场表达或短语，提供地道中文释义与用法小贴士。\n"
            "4. **绝对禁止**使用 Markdown 表格。请使用简单的加粗列表（如 `- **短语/单词**: 中文解释与用法`）来展示词汇。\n"
            "5. 严格使用 Markdown 格式排版，美观易读，开头不要有任何寒暄客套。"
        )
        user_prompt = (
            f"今日指定情境：【{scenario['name']}】\n"
            f"场景背景与要求：{scenario['context']}\n"
            f"请根据以上设定生成今日的实用英语阅读素材。"
        )
        return await ai_client.ask_ai(
            user_prompt,
            system=system_prompt,
            use_search=False,
            raise_on_failure=True,
        )

    async def generate_rss_reading(self) -> str:
        try:
            urls = [
                "https://feeds.npr.org/1004/rss.xml",  # NPR World
                "https://feeds.npr.org/1048/rss.xml",  # NPR Science
                "https://feeds.npr.org/1046/rss.xml",  # NPR Pop Culture
            ]
            url = random.choice(urls)
            items = await fetch_feed(
                FeedSource("Reading", url, "NPR"),
                max_age_seconds=None,
                max_items=10,
            )

            if not items:
                raise RuntimeError("NPR RSS 未返回文章")

            entry = random.choice(items)
            raw_text = f"Title: {entry.title}\nLink: {entry.url}\nSummary: {entry.summary}"

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
            return await ai_client.ask_ai(
                raw_text,
                system=system_prompt,
                use_search=False,
                raise_on_failure=True,
            )
        except Exception as e:
            logger.exception("抓取或生成 RSS 阅读失败: %s", e)
            raise

    async def generate_ted_reading(self) -> str:
        try:
            url = "https://pa.tedcdn.com/talks/rss"
            items = await fetch_feed(
                FeedSource("Reading", url, "TED"),
                max_age_seconds=None,
                max_items=20,
            )

            if not items:
                raise RuntimeError("TED RSS 未返回文章")

            entry = random.choice(items)
            title = entry.title or "Unknown TED Talk"
            link = entry.url or url
            summary = entry.summary
            raw_text = f"Title: {title}\nLink: {link}\nSummary: {summary}"

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
            return await ai_client.ask_ai(
                raw_text,
                system=system_prompt,
                use_search=False,
                raise_on_failure=True,
            )
        except Exception as e:
            logger.exception("抓取或生成 TED 阅读失败: %s", e)
            raise

    @reading_loop.before_loop
    async def before_reading_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="test_reading", description="[管理员] 立即测试每日英文阅读推送")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_reading(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在为您生成每日阅读材料，请稍等...", ephemeral=True)
        await self.reading_loop.coro(self)


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyReading(bot))
