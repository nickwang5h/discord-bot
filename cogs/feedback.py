"""Owner feedback on pushed items: per-item buttons, reactions, exposure sync and stats.

Discord interaction only; the one-verdict-per-item rules live in
`core.feedback.store.FeedbackStore`. Buttons are `DynamicItem`s parsed from their
`custom_id`, so they keep working on old messages after a restart. Only the bot owner's
clicks and reactions count; anyone else's are ignored without writing anything, and
shared channels never receive a reaction or reply from the bot.
"""
import asyncio
import logging
import threading
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, STATE_ROOT
from core.feedback import profile as reader_profile
from core.feedback.boards import LABELS
from core.feedback.store import DAY, FeedbackStore
from core.news.reader import DeliveryReader
from core.news.subscriptions import load_subscriptions
from core.news.topics import TOPICS

logger = logging.getLogger(__name__)

TEMPLATE = r'^fb:(?P<k>[nw]):(?P<ref>\d+):(?P<i>\d):(?P<v>new|known|skip|save)$'
KINDS = {'n': 'run', 'w': 'watch'}
LETTERS = {'n': 'n', 'w': 'w', 'run': 'n', 'watch': 'w'}
VALUES = ('new', 'known', 'skip', 'save')
VALUE_EMOJI = {'new': '🆕', 'known': '👌', 'skip': '🚫', 'save': '📥'}
REACTIONS = {'🆕': 'new', '👌': 'known', '🚫': 'skip'}
NUMBERS = '①②③④⑤'
MAX_ROWS = 5                # one row of four buttons per item; Discord allows five rows
STATS_ROWS = 15
BY_CHOICES = {'source': '来源', 'board': '板块', 'tag': '标签'}


class FeedbackButton(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    """One verdict (or 📥) button for item `index` of a news run (`n`) or watch delivery (`w`)."""

    def __init__(self, kind: str, ref: int, index: int, value: str, *, selected: bool = False):
        self.kind, self.ref, self.index, self.value = kind, int(ref), int(index), value
        super().__init__(
            discord.ui.Button(
                label=f'{NUMBERS[self.index]}{VALUE_EMOJI[value]}',
                style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
                custom_id=f'fb:{kind}:{self.ref}:{self.index}:{value}',
            ),
            row=self.index,
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        index = int(match['i'])
        if index >= MAX_ROWS:
            raise ValueError('按钮序号超出范围')
        return cls(match['k'], int(match['ref']), index, match['v'])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog('Feedback')
        if cog is None:
            await interaction.response.send_message('反馈功能未加载。', ephemeral=True)
            return
        await cog.handle_button(interaction, self.kind, self.ref, self.index, self.value)


def view_for(kind, ref, count, state=None):
    """Buttons for a message carrying `count` items (only the first five get a row).

    `kind` is `run`/`n` (news run id) or `watch`/`w` (watch delivery id). `state` maps an
    item position to `{'verdict': 'new'|'known'|'skip'|None, 'saved': bool}`; the selected
    verdict and a saved item show green. Returns None when there is nothing to attach.
    """
    letter = LETTERS[kind]
    count = min(int(count), MAX_ROWS)
    if count <= 0:
        return None
    view = discord.ui.View(timeout=None)
    for index in range(count):
        current = (state or {}).get(index) or {}
        for value in VALUES:
            selected = bool(current.get('saved')) if value == 'save' else current.get('verdict') == value
            view.add_item(FeedbackButton(letter, ref, index, value, selected=selected))
    return view


def _pick(items, index):
    """The item rendered at `index`: by stored position, or list order when positions are unknown."""
    if items and all(item.get('position') is not None for item in items):
        return next((item for item in items if item['position'] == index), None)
    return items[index] if index < len(items) else None


def _fmt(value):
    return f'{value:.1f}'.rstrip('0').rstrip('.') if value else '0'


def stats_line(bucket, label, weight=None):
    rate = bucket['new_rate']
    return (f'{label} · 曝光 {bucket["exposures"]} · 已评 {bucket["rated"]} · '
            f'🆕{_fmt(bucket["new"])} 👌{_fmt(bucket["known"])} 🚫{_fmt(bucket["skip"])} · '
            f'新知率 {"—" if rate is None else f"{rate:.0%}"}'
            + ('' if weight is None else f' · 权重 {weight:.2f}'))


def current_weight(profile, group, name):
    """The selection weight (§3.2) a stats row currently has; 1.0 without a profile."""
    if profile is None:
        return 1.0
    return {'source': profile.weight_for_source, 'board': profile.weight_for_board,
            'tag': profile.weight_for_tag}[group](name)


class Feedback(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._store = None
        self._reader = None
        self._open_lock = threading.Lock()
        self._last_cleanup = 0.0
        if SCHEDULED_JOBS_ENABLED:
            self.sync_loop.start()

    # ---- lifecycle ------------------------------------------------------------------

    async def cog_load(self):
        self.bot.add_dynamic_items(FeedbackButton)

    async def cog_unload(self):
        task = self.sync_loop.get_task()
        self.sync_loop.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        self.bot.remove_dynamic_items(FeedbackButton)
        if self._store is not None:
            self._store.close()
            self._store = None

    @property
    def store(self) -> FeedbackStore:
        # Opened on first use (possibly from a worker thread), never at import or load.
        with self._open_lock:
            if self._store is None:
                self._store = FeedbackStore(STATE_ROOT / 'data' / 'feedback.sqlite3')
            return self._store

    @property
    def reader(self) -> DeliveryReader:
        if self._reader is None:
            self._reader = DeliveryReader(STATE_ROOT / 'data' / 'news.sqlite3')
        return self._reader

    @staticmethod
    def _subscriptions():
        return load_subscriptions(TOPICS)[0]

    def _inbox(self):
        cog = self.bot.get_cog('Inbox')
        return getattr(cog, 'store', None)

    async def _is_owner(self, user_id: int) -> bool:
        return await self.bot.is_owner(discord.Object(id=user_id))

    # ---- exposure sync --------------------------------------------------------------

    def _sync_once(self, now=None):
        now = time.time() if now is None else now
        added = self.store.sync_exposures(self.reader, self._subscriptions())
        if now - self._last_cleanup >= DAY:
            self.store.cleanup(now=now)
            self._last_cleanup = now
        return added

    @tasks.loop(minutes=10)
    async def sync_loop(self):
        try:
            added = await asyncio.to_thread(self._sync_once)
            if added:
                logger.info('反馈曝光同步：新增 %s 条', added)
        except Exception:
            logger.exception('反馈曝光同步失败')

    @sync_loop.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    # ---- buttons --------------------------------------------------------------------

    def _items(self, kind, ref):
        return self.store.items_for_ref(KINDS[kind], ref, reader=self.reader, subscriptions=self._subscriptions())

    def _state(self, items):
        keys = [item['key'] for item in items]
        verdicts = self.store.verdicts(keys)
        state = {}
        for order, item in enumerate(items):
            position = item['position'] if item.get('position') is not None else order
            current = verdicts.get(item['key'])
            # Only an exact verdict shows as selected; pressing it again is the undo.
            state[position] = {'verdict': current['verdict'] if current and not current['coarse'] else None,
                               'saved': self.store.saved(item['key']) is not None}
        return state

    async def _redraw(self, interaction, kind, ref, items, *, deferred):
        try:
            state = await asyncio.to_thread(self._state, items)
            count = max([item.get('item_count') or 0 for item in items] + [len(items)])
            view = view_for(kind, ref, count, state)
            if deferred:
                await interaction.edit_original_response(view=view)
            else:
                await interaction.response.edit_message(view=view)
        except Exception as error:  # noqa: BLE001 - the verdict is already stored
            logger.warning('反馈按钮视图更新失败 [%s:%s]: %s', kind, ref, error)

    async def handle_button(self, interaction: discord.Interaction, kind, ref, index, value):
        if not await self._is_owner(interaction.user.id):
            await interaction.response.send_message('仅所有者可用。', ephemeral=True)
            return
        try:
            items = await asyncio.to_thread(self._items, kind, ref)
        except Exception:
            logger.exception('反馈条目查询失败 [%s:%s]', kind, ref)
            await interaction.response.send_message('反馈暂时不可用，请稍后再试。', ephemeral=True)
            return
        item = _pick(items, index)
        if item is None:
            await interaction.response.send_message('这条已无法反馈。', ephemeral=True)
            return
        if value == 'save':
            await self._save(interaction, kind, ref, items, item)
            return
        message_id = interaction.message.id if interaction.message else None
        try:
            await asyncio.to_thread(self.store.record, item['key'], value, via='button', message_id=message_id)
        except Exception:
            logger.exception('反馈写入失败 [%s]', item['key'])
            await interaction.response.send_message('反馈保存失败，请稍后再试。', ephemeral=True)
            return
        await self._redraw(interaction, kind, ref, items, deferred=False)

    async def _save(self, interaction, kind, ref, items, item):
        inbox = self.bot.get_cog('Inbox')
        if inbox is None:
            await interaction.response.send_message('收件箱未加载，无法保存。', ephemeral=True)
            return
        from cogs.inbox import Payload

        # Fetching the article can take longer than the three-second interaction window.
        await interaction.response.defer()
        title = item.get('title_zh') or item.get('title') or item['url']
        summary = item['title'] if item.get('title_zh') and item.get('title') != title else ''
        payload = Payload(url=item['url'], title=title, note='', summary=summary)
        origin = interaction.message.jump_url if interaction.message else ''
        try:
            saved = await inbox.save_payload(payload, origin=origin, fallback=interaction.channel,
                                             source=item.get('source'), via='button')
            await asyncio.to_thread(self.store.record_save, item['key'], saved['id'])
        except Exception:
            logger.exception('📥 保存失败 [%s]', item['key'])
            await interaction.followup.send('保存到收件箱失败，请稍后再试。', ephemeral=True)
            return
        await self._redraw(interaction, kind, ref, items, deferred=True)

    # ---- reactions ------------------------------------------------------------------

    def _message_items(self, message_id):
        return self.store.items_for_message(message_id, reader=self.reader, subscriptions=self._subscriptions(),
                                            inbox=self._inbox())

    def _apply_reaction(self, message_id, emoji, *, added):
        keys = list(dict.fromkeys(item['key'] for item in self._message_items(message_id)))
        if not keys:
            return 0
        # One item: an exact verdict. Several: a coarse verdict shared out at 1/n each.
        verdict, coarse = REACTIONS[emoji], len(keys) > 1
        weight = 1.0 / len(keys) if coarse else 1.0
        for key in keys:
            if added:
                self.store.record(key, verdict, via='reaction', weight=weight, coarse=coarse,
                                  message_id=message_id, emoji=emoji)
            else:
                self.store.undo(key, via='reaction', message_id=message_id, emoji=emoji)
        return len(keys)

    async def _reaction(self, event: discord.RawReactionActionEvent, *, added):
        emoji = str(event.emoji)
        if emoji not in REACTIONS or not await self._is_owner(event.user_id):
            return
        # Add events say whose message it is; removals do not, but only bot messages map to items.
        author = getattr(event, 'message_author_id', None)
        if added and author is not None and self.bot.user is not None and author != self.bot.user.id:
            return
        try:
            await asyncio.to_thread(self._apply_reaction, event.message_id, emoji, added=added)
        except Exception:
            logger.exception('反应反馈处理失败 [%s]', event.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, event: discord.RawReactionActionEvent):
        await self._reaction(event, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, event: discord.RawReactionActionEvent):
        await self._reaction(event, added=False)

    # ---- statistics -----------------------------------------------------------------

    @app_commands.command(name='feedback_stats', description='[管理员] 查看反馈统计：曝光、已评与新知率（仅所有者）')
    @app_commands.describe(days='统计最近多少天（7–365，默认 30）', by='分组方式')
    @app_commands.choices(by=[app_commands.Choice(name=label, value=key) for key, label in BY_CHOICES.items()])
    @app_commands.default_permissions(administrator=True)
    async def feedback_stats(self, interaction: discord.Interaction,
                             days: app_commands.Range[int, 7, 365] = 30,
                             by: app_commands.Choice[str] | None = None):
        if not await self._is_owner(interaction.user.id):
            await interaction.response.send_message('反馈统计只对机器人所有者开放。', ephemeral=True)
            return
        group = by.value if by else 'source'
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(self.store.stats, time.time() - days * DAY, by=group)
        profile = await asyncio.to_thread(self.current_profile)
        await interaction.followup.send(self.render_stats(result, days, group, profile), ephemeral=True,
                                        embed=self.profile_embed(profile))

    def current_profile(self, now=None):
        """The profile `following` selection uses right now (same window), or None."""
        now = time.time() if now is None else now
        try:
            return reader_profile.build(self.store.feedback_since(now - reader_profile.WINDOW_DAYS * DAY), now)
        except Exception:
            logger.exception('读者画像计算失败')
            return None

    @staticmethod
    def profile_embed(profile):
        # Up to ~2,700 characters: an embed description (4,096) fits it, a message (2,000) does not.
        lines = reader_profile.explain(profile).split('\n')
        if profile is not None:
            lines[-1] = f'```json\n{lines[-1]}\n```'
        return discord.Embed(title='当前发给模型的画像片段', description='\n'.join(lines)[:4000])

    @staticmethod
    def render_stats(result, days, group, profile=None):
        totals = result['totals']
        lines = [f'**反馈统计 · 最近 {days} 天 · 按{BY_CHOICES[group]}**', stats_line(totals, '合计')]
        if not totals['exposures'] and not totals['rated']:
            lines.append('还没有曝光或反馈记录。')
        for bucket in result['rows'][:STATS_ROWS]:
            name = LABELS.get(bucket['name'], bucket['name']) if group == 'board' else bucket['name']
            weight = current_weight(profile, group, bucket['name'])
            lines.append(stats_line(bucket, f'`{str(name)[:40]}`', weight))
        if len(result['rows']) > STATS_ROWS:
            lines.append(f'……另有 {len(result["rows"]) - STATS_ROWS} 组')
        return '\n'.join(lines)[:1900]


async def setup(bot: commands.Bot):
    await bot.add_cog(Feedback(bot))
