"""Discord commands and timers only; all news business rules live in core.news."""
import asyncio
import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, STATE_ROOT, TZ
from core import settings
from core.news import personal
from core.news.pipeline import NewsPipeline
from core.news.sources import SHARED_NAMES, personal_entries
from core.news.store import NewsStore
from core.news.subscriptions import edition_name, load_subscriptions, period_for
from core.news.topics import TOPICS

logger = logging.getLogger(__name__)


class News(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pipeline = None
        self._active: set[asyncio.Task] = set()
        self._scheduled: dict[str, asyncio.Task] = {}
        self._skip_initial_fetch = True
        if SCHEDULED_JOBS_ENABLED:
            self.hourly_fetch.start()
            self.dispatch.start()

    @property
    def pipeline(self):
        if self._pipeline is None:
            store = NewsStore(STATE_ROOT / 'data' / 'news.sqlite3')
            try:
                self._pipeline = NewsPipeline(store, limits=settings.get_setting('NEWS_LIMITS', {}))
            except Exception:
                store.close()
                raise
        return self._pipeline

    async def cog_unload(self):
        # Cancellation reaches the pipeline before the DB closes, preserving send uncertainty.
        running = [task for loop in (self.hourly_fetch, self.dispatch) if (task := loop.get_task()) is not None]
        self.hourly_fetch.cancel()
        self.dispatch.cancel()
        active = (set(self._active) | set(self._scheduled.values())) - {asyncio.current_task()}
        for task in active:
            task.cancel()
        if running or active:
            await asyncio.gather(*running, *active, return_exceptions=True)
        if self._pipeline is not None:
            self._pipeline.store.close()

    @tasks.loop(minutes=60)
    async def hourly_fetch(self):
        if self._skip_initial_fetch:
            self._skip_initial_fetch = False
            return
        try:
            await self.pipeline.ingester.collect()
        except Exception:
            logger.exception('新闻素材采集失败')

    @tasks.loop(seconds=30)
    async def dispatch(self):
        try:
            subscriptions, errors = load_subscriptions(TOPICS)
            for error in errors:
                logger.warning('新闻订阅配置错误: %s', error)
            now = datetime.datetime.now(TZ)
            due = [s for s in subscriptions if s.enabled and s.channel_id and period_for(s, now, scheduled=True)]
            if due and self.pipeline.store.ready:
                for sub in due:
                    previous = self._scheduled.get(sub.id)
                    if previous is None or previous.done():
                        self._scheduled[sub.id] = asyncio.create_task(
                            self.pipeline.publish_due([sub], now, self.bot.get_channel))
                # Drop completed tasks for subscriptions removed from the configuration.
                self._scheduled = {key: task for key, task in self._scheduled.items() if not task.done()}
        except Exception:
            logger.exception('新闻调度失败')

    @hourly_fetch.before_loop
    @dispatch.before_loop
    async def before_news(self):
        await self.bot.wait_until_ready()

    def _subscription(self, identity):
        subscriptions, _ = load_subscriptions(TOPICS)
        for subscription in subscriptions:
            if subscription.id == identity:
                return subscription
        raise ValueError('找不到有效订阅配置')

    def _channel(self, interaction, channel_id):
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if (channel is None or interaction.guild is None
                or getattr(channel, 'guild', None) != interaction.guild):
            raise ValueError('订阅没有配置到当前服务器的可用频道')
        return channel

    def _track_command(self):
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        task.add_done_callback(self._active.discard)

    async def _execute(self, interaction, identity, *, preview=False, current_channel=False):
        self._track_command()
        await interaction.response.defer(ephemeral=True)
        subscription = self._subscription(identity)
        target = interaction.channel if current_channel else self._channel(interaction, subscription.channel_id)
        if target is None:
            raise ValueError('当前上下文没有可用频道')
        period = period_for(subscription, datetime.datetime.now(TZ))
        if preview:
            edition = await self.pipeline.preview(subscription, period)
            if edition:
                await interaction.followup.send(embeds=edition.embeds, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
                return
        else:
            if not subscription.enabled:
                raise ValueError('该订阅已停用')
            edition = await self.pipeline.publish(subscription, period, target)
            if edition:
                await interaction.followup.send(f'{edition_name(period)}已发送。', ephemeral=True)
                return
        await interaction.followup.send('未发送：无合适新内容、已有运行、本期已处理或存在待核查发送。可用 /news_status 查看。', ephemeral=True)

    @app_commands.command(name='news_preview', description='[管理员] 预览新闻订阅，不修改正式投递历史')
    @app_commands.checks.has_permissions(administrator=True)
    async def news_preview(self, interaction: discord.Interaction, subscription: str):
        await self._execute(interaction, subscription, preview=True)

    @app_commands.command(name='news_publish', description='[管理员] 发布订阅本期新闻到其配置频道')
    @app_commands.checks.has_permissions(administrator=True)
    async def news_publish(self, interaction: discord.Interaction, subscription: str):
        await self._execute(interaction, subscription)

    @app_commands.command(name='news_status', description='[管理员] 查看新闻订阅及待核查发送')
    @app_commands.checks.has_permissions(administrator=True)
    async def news_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        subscriptions, errors = load_subscriptions(TOPICS)
        lines = [f'历史初始化：{"完成" if self.pipeline.store.ready else "未完成（正式发送已阻止）"}']
        for sub in subscriptions:
            channel = self.bot.get_channel(sub.channel_id) if sub.channel_id else None
            if channel is not None and getattr(channel, 'guild', None) != interaction.guild:
                continue
            status = self.pipeline.store.status(sub.id)
            latest = status['latest']
            lines.append(f'{sub.id} ({sub.topic}) · {",".join(sub.times)} · '
                         f'{"启用" if sub.enabled else "停用"} · {latest["status"] if latest else "尚未运行"}'
                         + (f' · 待核查 #{status["pending"]}' if status['pending'] else ''))
        lines.extend(errors)
        await interaction.followup.send('\n'.join(lines)[:1900], ephemeral=True)

    @app_commands.command(name='news_resolve', description='[管理员] 人工核查后填写消息ID确认送达，或 skip 放弃本次内容')
    @app_commands.checks.has_permissions(administrator=True)
    async def news_resolve(self, interaction: discord.Interaction, run_id: int, message_id_or_skip: str):
        self._track_command()
        await interaction.response.defer(ephemeral=True)
        run = self.pipeline.store.get_run(run_id)
        if run is None or run['status'] != 'uncertain':
            raise ValueError('该运行不是待核查状态')
        channel = self._channel(interaction, int(run['channel_id']))
        if message_id_or_skip == 'skip':
            self.pipeline.store.complete(run_id, None, status='skipped')
        else:
            if not message_id_or_skip.isdecimal() or len(message_id_or_skip) > 20:
                raise ValueError('消息 ID 无效')
            message = await channel.fetch_message(int(message_id_or_skip))
            if message.author != self.bot.user or message.created_at.timestamp() < run['created_at']:
                raise ValueError('消息必须由本 Bot 在该次运行后发送；请重新核查')
            self.pipeline.store.complete(run_id, message.id)
        await interaction.followup.send('核查状态已记录；不会自动补发本次内容。', ephemeral=True)

    @app_commands.command(name='test_news', description='[管理员] 立即测试新闻推送（私密预览）')
    @app_commands.checks.has_permissions(administrator=True)
    async def test_news(self, interaction: discord.Interaction):
        await self._execute(interaction, 'general', preview=True, current_channel=True)

    @app_commands.command(name='test_hourly_fetch', description='[实验] 手动触发一次探索素材抓取')
    @app_commands.checks.has_permissions(administrator=True)
    async def test_hourly_fetch(self, interaction: discord.Interaction):
        self._track_command()
        await interaction.response.defer(ephemeral=True)
        await self.pipeline.ingester.collect()
        await interaction.followup.send('共享素材收集完成（受一小时采集间隔限制）；未调用模型。', ephemeral=True)

    @app_commands.command(name='test_scheduled_digest', description='[实验] 手动触发一次视野拾遗推送')
    @app_commands.checks.has_permissions(administrator=True)
    async def test_scheduled_digest(self, interaction: discord.Interaction):
        await self._execute(interaction, 'discovery', current_channel=True)

    # Personal feed list. Owner-only: these sources reach only the owner's inbox channel.
    async def _owner_only(self, interaction):
        if await self.bot.is_owner(discord.Object(id=interaction.user.id)):
            return True
        await interaction.response.send_message('个人信源只对机器人所有者开放。', ephemeral=True)
        return False

    @app_commands.command(name='source_list', description='[管理员] 列出个人信源（仅所有者，只投递到收件箱）')
    async def source_list(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        entries, errors = await asyncio.to_thread(personal_entries)
        lines = [f'**个人信源 {len(entries)} 个**（订阅 following → 收件箱频道）']
        for section in personal.SECTIONS:
            names = [f'`{e["name"]}`' + (' ·RSSHub' if 'rsshub' in e else '')
                     for e in entries if e['category'] == section]
            lines.append(f'**{section}**：' + ('、'.join(names) if names else '（无）'))
        lines.extend(errors)
        await interaction.response.send_message('\n'.join(lines)[:1900], ephemeral=True)

    @app_commands.command(name='source_add', description='[管理员] 新增个人信源：RSSHub 路由或 https RSS 地址（仅所有者）')
    @app_commands.describe(name='显示名称（唯一）', address='RSSHub 路由如 /twitter/user/名字，或 https 公网 RSS 地址',
                           section='所属板块')
    @app_commands.choices(section=[app_commands.Choice(name=s, value=s) for s in personal.SECTIONS])
    async def source_add(self, interaction: discord.Interaction, name: str, address: str,
                         section: app_commands.Choice[str]):
        if not await self._owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            entry = personal.entry_from_input(name, address, section.value)
            count = await personal.probe(entry)
            await asyncio.to_thread(personal.add, entry, SHARED_NAMES)
        except (ValueError, personal.ProbeError) as error:
            await interaction.followup.send(f'未添加：{error}', ephemeral=True)
            return
        await interaction.followup.send(
            f'✅ 已添加 `{entry["name"]}`（{entry["category"]}），试取到 {count} 条；下次采集生效，无需重启。',
            ephemeral=True)

    @app_commands.command(name='source_remove', description='[管理员] 删除个人信源（仅所有者）')
    @app_commands.describe(name='要删除的信源名称')
    async def source_remove(self, interaction: discord.Interaction, name: str):
        if not await self._owner_only(interaction):
            return
        try:
            await asyncio.to_thread(personal.remove, name)
        except ValueError as error:
            await interaction.response.send_message(f'未删除：{error}', ephemeral=True)
            return
        await interaction.response.send_message(f'🗑️ 已删除 `{name}`；已采集的素材不再进入后续选编。', ephemeral=True)

    @source_remove.autocomplete('name')
    async def _source_names(self, interaction: discord.Interaction, current: str):
        if not await self.bot.is_owner(discord.Object(id=interaction.user.id)):
            return []
        entries, _ = await asyncio.to_thread(personal_entries)
        return [app_commands.Choice(name=e['name'], value=e['name'])
                for e in entries if current.casefold() in e['name'].casefold()][:25]


async def setup(bot):
    await bot.add_cog(News(bot))
