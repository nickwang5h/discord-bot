"""Knowledge loop: /recall, /review, index sync and the weekly review delivery.

Discord interaction and timers only. Retrieval and answering live in
`core.knowledge.recall`, statistics in `core.knowledge.review`, storage in
`core.knowledge.store`. Every SQLite call runs in `asyncio.to_thread`; the store is
opened on first use, never at import or load, and closed on unload.

- `sync_loop` (30 min, offset 5 min from the feedback sync's 10-minute grid) copies
  news material, inbox entries and pins into the index; `maintenance` (03:40) applies
  the retention policy.
- `weekly_report` (Monday 09:00, `BOT_TIMEZONE`) posts last week's review to
  `INBOX_CHANNEL_ID` at most once: the ISO week is claimed in `reports`, the intent is
  stored before the single `channel.send`, and a failed or interrupted send is
  `uncertain` and never repeated. A missed slot (bot offline) is not caught up.
"""
import asyncio
import dataclasses
import datetime
import logging
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, STATE_ROOT, TZ
from core import settings
from core.feedback.boards import source_catalog
from core.jobs import RetryPolicy, run_delivery_job
from core.knowledge import review as weekly
from core.knowledge.recall import RecallError, RecallResult, recall
from core.knowledge.store import DAY, KnowledgeStore
from core.knowledge.sync import sync_all
from core.news.reader import PoolReader

logger = logging.getLogger(__name__)

SYNC_OFFSET_SECONDS = 5 * 60
REPORT_WEEKDAY = 0  # Monday
REPORT_TIME = datetime.time(hour=9, minute=0, tzinfo=TZ)
MAINTENANCE_TIME = datetime.time(hour=3, minute=40, tzinfo=TZ)
REPORT_RETRY = RetryPolicy(attempts=2, initial_delay_seconds=30)
SCOPE_CHOICES = {'all': '全部', 'news': '新闻', 'inbox': '收藏'}
EMBED_CHARS = 3800


class Knowledge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._store = None
        self._pool = None
        self._open_lock = threading.Lock()
        self._recall_slots = asyncio.Semaphore(1)
        self._report_lock = asyncio.Lock()
        self.last_sync: dict | None = None
        if SCHEDULED_JOBS_ENABLED:
            self.sync_loop.start()
            self.maintenance.start()
            self.weekly_report.start()

    # ---- lifecycle ------------------------------------------------------------------

    def _loops(self):
        return (self.sync_loop, self.maintenance, self.weekly_report)

    async def cog_unload(self):
        running = [task for loop in self._loops() if (task := loop.get_task()) is not None]
        for loop in self._loops():
            loop.cancel()
        running = [task for task in running if task is not asyncio.current_task()]
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        with self._open_lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    @property
    def store(self) -> KnowledgeStore:
        # Opened on first use (possibly from a worker thread), never at import or load.
        with self._open_lock:
            if self._store is None:
                self._store = KnowledgeStore(STATE_ROOT / 'data' / 'knowledge.sqlite3')
            return self._store

    @property
    def pool(self) -> PoolReader:
        if self._pool is None:
            self._pool = PoolReader(STATE_ROOT / 'data' / 'news.sqlite3')
        return self._pool

    def _inbox(self):
        return getattr(self.bot.get_cog('Inbox'), 'store', None)

    def _feedback(self):
        cog = self.bot.get_cog('Feedback')
        return cog.store if cog is not None else None

    async def _is_owner(self, user_id: int) -> bool:
        return await self.bot.is_owner(discord.Object(id=user_id))

    @staticmethod
    def _channel_id():
        value = settings.get_setting('INBOX_CHANNEL_ID')
        return int(value) if value and str(value).isdecimal() else None

    # ---- sync and maintenance -------------------------------------------------------

    def _sync_once(self, now=None):
        """One sync round (worker thread); a missing Inbox/Feedback cog skips its steps."""
        result = sync_all(self.store, pool_reader=self.pool, inbox_store=self._inbox(),
                          feedback_store=self._feedback(), now=now)
        self.last_sync = {'at': time.time() if now is None else now, 'errors': dict(result['errors'])}
        return result

    @tasks.loop(minutes=30)
    async def sync_loop(self):
        try:
            result = await asyncio.to_thread(self._sync_once)
            news, inbox = result['news'] or {}, result['inbox'] or {}
            if news.get('changed') or inbox.get('changed') or result['pins']:
                logger.info('知识库同步：新闻 %s · 收藏 %s · pin %s', news.get('changed', 0),
                            inbox.get('changed', 0), result['pins'] or 0)
        except Exception:
            logger.exception('知识库同步失败')

    @sync_loop.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()
        # The feedback exposure sync runs on a 10-minute grid from ready; stay between its ticks.
        await asyncio.sleep(SYNC_OFFSET_SECONDS)

    @tasks.loop(time=MAINTENANCE_TIME)
    async def maintenance(self):
        try:
            result = await asyncio.to_thread(self.store.cleanup)
            logger.info('知识库清理：%s', result)
        except Exception:
            logger.exception('知识库清理失败')

    # ---- weekly review --------------------------------------------------------------

    def build_review(self, week, now=None):
        """The review of `week` from the stores that are loaded (worker thread)."""
        try:
            catalog = source_catalog()
        except Exception:  # noqa: BLE001 - the lists that need it are optional
            logger.exception('信源清单读取失败，周报不列未推送和疑似失效的信源')
            catalog = None
        return weekly.build_review(week, feedback=self._feedback(), inbox=self._inbox(), knowledge=self.store,
                                   catalog=catalog, now=now)

    @staticmethod
    def review_embed(result) -> discord.Embed:
        embed = discord.Embed(title=result.title, description=result.render(), color=discord.Color.teal())
        embed.set_footer(text='纯统计，未调用模型 · /review 可查看任意一周')
        return embed

    async def deliver_weekly(self, now=None):
        """Post last week's review once. Returns the sent embed, or None when skipped."""
        now = now or self._now()
        channel_id = self._channel_id()
        if channel_id is None:
            logger.warning('未设置 INBOX_CHANNEL_ID，跳过每周回看')
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.error('找不到收件箱频道 %s，跳过每周回看', channel_id)
            return None
        week = weekly.previous_week(now)
        state = {'phase': None, 'message': None}

        async def build():
            if state['phase'] is None:
                if not await asyncio.to_thread(self.store.claim_report, week.key, channel_id):
                    logger.info('每周回看 %s 已处理过，不再发送', week.key)
                    return None
                state['phase'] = 'claimed'
            result = await asyncio.to_thread(self.build_review, week, now.timestamp())
            return self.review_embed(result)

        async def deliver(embed):
            await asyncio.to_thread(self.store.report_intent, week.key,
                                    {'title': embed.title, 'description': embed.description})
            state['phase'] = 'sending'
            state['message'] = await channel.send(embed=embed)
            state['phase'] = 'sent'

        async def delivered(_embed):
            await asyncio.to_thread(self.store.complete_report, week.key, state['message'].id)

        try:
            return await run_delivery_job(lock=self._report_lock, task_name=f'每周回看 {week.key}', build=build,
                                          deliver=deliver, on_delivered=delivered, retry_policy=REPORT_RETRY)
        except BaseException:
            # Never resend: a build failure is final, an interrupted send is uncertain.
            final = {'claimed': 'failed', 'sending': 'uncertain'}.get(state['phase'])
            if final is not None:
                try:
                    await asyncio.to_thread(self.store.complete_report, week.key, None, status=final)
                except Exception:  # noqa: BLE001 - the store already turns `sending` into uncertain on restart
                    logger.exception('每周回看状态记录失败 [%s]', week.key)
            raise

    @staticmethod
    def _now():
        return datetime.datetime.now(TZ)

    @tasks.loop(time=REPORT_TIME)
    async def weekly_report(self):
        now = self._now()
        if now.weekday() != REPORT_WEEKDAY:
            return
        try:
            await self.deliver_weekly(now)
        except Exception:
            logger.exception('每周回看投递失败')

    @maintenance.before_loop
    @weekly_report.before_loop
    async def before_scheduled(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='review', description='[管理员] 查看每周回看：新知/已知、来源、收件箱（仅所有者，私密）')
    @app_commands.describe(week='ISO 周，如 2026-W41；默认上一周')
    @app_commands.default_permissions(administrator=True)
    async def review(self, interaction: discord.Interaction, week: str | None = None):
        if not await self._is_owner(interaction.user.id):
            await interaction.response.send_message('每周回看只对机器人所有者开放。', ephemeral=True)
            return
        try:
            target = weekly.parse_week(week) if week else weekly.previous_week()
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(self.build_review, target)
            report = await asyncio.to_thread(self.store.get_report, target.key)
        except Exception:
            logger.exception('/review 统计失败 [%s]', target.key)
            await interaction.followup.send('周报统计失败，请稍后再试。', ephemeral=True)
            return
        embed = self.review_embed(result)
        status = report['status'] if report else '未投递'
        embed.set_footer(text=f'私密预览，不占用定时周报（该周定时周报：{status}）· 未调用模型')
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /recall --------------------------------------------------------------------

    def _with_inbox_links(self, result: RecallResult, guild_id=None) -> RecallResult:
        """Inbox sources without a URL link to where they were saved (worker thread)."""
        inbox = self._inbox()
        if inbox is None or not any(s.kind == 'inbox' and not s.url for s in result.sources):
            return result
        sources = []
        for source in result.sources:
            if source.kind == 'inbox' and not source.url:
                item = inbox.get(source.doc_id.removeprefix('inbox:')) or {}
                link = item.get('origin') or ''
                if not link.startswith('https://') and guild_id and item.get('card_message_id'):
                    link = (f'https://discord.com/channels/{guild_id}/{item["card_channel_id"]}/'
                            f'{item["card_message_id"]}')
                if link.startswith('https://'):
                    source = dataclasses.replace(source, url=link)
            sources.append(source)
        return dataclasses.replace(result, sources=tuple(sources))

    @staticmethod
    def recall_embed(question: str, body: str, *, footer: str, failed: bool = False) -> discord.Embed:
        embed = discord.Embed(title=f'🧠 {question[:240]}', description=body[:EMBED_CHARS],
                              color=discord.Color.orange() if failed else discord.Color.blue())
        embed.set_footer(text=footer[:300])
        return embed

    @staticmethod
    def _footer(result: RecallResult) -> str:
        parts = [f'本地命中 {result.hits} 条']
        if result.attribution:
            parts.append(f'作答：{result.attribution}')
        elif result.status != 'ok':
            parts.append('未调用作答模型')
        if result.plan_fallback:
            parts.append('检索词为确定性回退')
        return ' · '.join(parts)

    @app_commands.command(name='recall', description='[管理员] 从自己的收藏和新闻存档里回忆并作答（仅所有者，私密）')
    @app_commands.describe(question='想回忆的问题（≤300 字）', scope='检索范围，默认全部', days='回看多少天（7–365，默认 90）')
    @app_commands.choices(scope=[app_commands.Choice(name=label, value=key) for key, label in SCOPE_CHOICES.items()])
    @app_commands.default_permissions(administrator=True)
    async def recall_command(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 300],
                             scope: app_commands.Choice[str] | None = None,
                             days: app_commands.Range[int, 7, 365] = 90):
        if not await self._is_owner(interaction.user.id):
            await interaction.response.send_message('/recall 只对机器人所有者开放。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self._recall_slots:
            try:
                await asyncio.to_thread(self.store.bump_usage, weekly.RECALL_USAGE,
                                        datetime.datetime.now(TZ).date().isoformat())
            except Exception:  # noqa: BLE001 - a usage counter never blocks the answer
                logger.warning('/recall 用量计数失败', exc_info=True)
            try:
                result = await recall(question, self.store, scope=scope.value if scope else None, days=days,
                                      limits=settings.get_setting('RECALL_LIMITS'))
            except RecallError as error:
                fallback = error.fallback
                body = f'⚠️ {error}'
                footer = '作答模型失败，以下为零模型兜底'
                if fallback is not None:
                    fallback = await asyncio.to_thread(self._with_inbox_links, fallback, interaction.guild_id)
                    body = f'{body}\n\n{fallback.render(max_chars=EMBED_CHARS - len(body) - 2)}'
                    footer = f'{self._footer(fallback)} · {footer}'
                await interaction.followup.send(embed=self.recall_embed(question, body, footer=footer, failed=True),
                                                ephemeral=True)
                return
            except ValueError as error:
                await interaction.followup.send(f'无法检索：{error}', ephemeral=True)
                return
            except Exception:
                logger.exception('/recall 失败')
                await interaction.followup.send('回忆失败，请稍后再试。', ephemeral=True)
                return
        result = await asyncio.to_thread(self._with_inbox_links, result, interaction.guild_id)
        await interaction.followup.send(
            embed=self.recall_embed(question, result.render(max_chars=EMBED_CHARS), footer=self._footer(result),
                                    failed=result.status not in {'ok', 'no_results'}),
            ephemeral=True)

    # ---- health ---------------------------------------------------------------------

    def stats_line(self) -> str:
        """One line for /health (worker thread). Never opens the database itself: the first
        open may rebuild the FTS index, which is too slow for an interaction reply."""
        store = self._store
        if store is None:
            return '- 知识库: ➖ 尚未打开（首次同步或 /recall 后显示）'
        stats = store.stats()
        synced = '尚未同步' if self.last_sync is None else (
            '最近同步有错误' if self.last_sync['errors'] else '最近同步正常')
        return (f"- 知识库: {stats['docs']:,} 篇（新闻 {stats['news']:,} · 收藏 {stats['inbox']:,}）"
                f" · {stats['bytes'] / 1_048_576:.1f} MB · 全文检索 {'✅' if stats['fts5'] else '⚠️ 不可用'} · {synced}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Knowledge(bot))
