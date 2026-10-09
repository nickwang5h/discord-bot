"""One bounded path for every topic, with persistent send intent and per-subscription locks."""
import asyncio
import datetime
import json
import logging

from core import ai_client
from core.jobs import RetryPolicy, run_delivery_job
from core.news.ingest import Ingester
from core.news.models import Edition, fingerprint
from core.news.sources import sources_for
from core.news.subscriptions import bounded_int, edition_name
from core.news.topics import TOPICS

logger = logging.getLogger(__name__)
MAX_INPUT_CHARS = 60000
MAX_RESULT_CHARS = 24000


class NewsPipeline:
    def __init__(self, store, *, topics=None, limits=None):
        self.store = store
        self.topics = TOPICS if topics is None else topics
        self.ingester = Ingester(store)
        self.locks = {}
        limits = {} if limits is None else limits
        if not isinstance(limits, dict) or set(limits) - {'concurrency', 'daily_calls', 'daily_output_tokens'}:
            raise ValueError('NEWS_LIMITS 配置无效')
        self.model_slots = asyncio.Semaphore(bounded_int(limits.get('concurrency', 2), 1, 2))
        self.max_calls = bounded_int(limits.get('daily_calls', 24), 1, 48)
        self.max_tokens = bounded_int(limits.get('daily_output_tokens', 72000), 3000, 144000)

    async def build(self, subscription, period, *, preview=False):
        topic = self.topics[subscription.topic]
        topic.validate_params(subscription.params)
        await self.ingester.collect()
        history = [] if preview else self.store.history(subscription.id)
        articles = self.store.articles({s.name for s in sources_for(subscription.source_groups)}, age=topic.max_age)
        if not preview:
            articles = [a for a in articles if not self.store.seen(subscription.id, {'url': a.url, '_version': a.version})]
        prepared = topic.prepare(articles, subscription, history, edition_name(period))
        if not prepared.candidates:
            return None
        # Include history and scope: selection is contextual, not a universal article summary.
        key = fingerprint([topic.name, topic.version, subscription.params, subscription.source_groups,
                           subscription.max_candidates, subscription.max_output_tokens, prepared.data,
                           [(c['_article_id'], c['_version']) for c in prepared.candidates]])
        raw = json.dumps(prepared.data, ensure_ascii=False, separators=(',', ':'))
        if len(raw) > MAX_INPUT_CHARS:
            raise ValueError('新闻模型输入超过全局预算')
        cached = self.store.cached(key)
        if cached:
            text, attribution = cached
        else:
            async with self.model_slots:
                day = datetime.datetime.now(datetime.UTC).date().isoformat()
                if not self.store.reserve_budget(day, subscription.max_output_tokens,
                        max_calls=self.max_calls, max_tokens=self.max_tokens):
                    raise RuntimeError('新闻今日模型预算已用尽')
                async with asyncio.timeout(180):
                    result = await ai_client.generate_ai(raw, system=prepared.system, use_search=False,
                        json_mode=True, max_output_tokens=subscription.max_output_tokens)
                text, attribution = result.text, result.attribution
        if not isinstance(text, str) or len(text) > MAX_RESULT_CHARS:
            raise ValueError('新闻模型结果超过容量')
        selected = topic.validate(text, prepared.candidates, subscription, history)
        # Rendering also validates the complete message before caching or committing a send intent.
        if selected:
            topic.render(selected, edition_name(period), attribution)
        if not cached:
            self.store.cache(key, topic.name, topic.version, subscription.params, text, attribution,
                             prepared.candidates, selected)
        publishable = []
        identities = set()
        for item in selected:
            identity = topic.identity(item)
            if identity in identities or (not preview and self.store.seen(subscription.id, item, identity)):
                continue
            identities.add(identity)
            publishable.append({**item, '_delivery_key': identity})
        if not publishable:
            return None
        embeds = topic.render(publishable, edition_name(period), attribution)
        if not 1 <= len(embeds) <= 10 or sum(len(embed) for embed in embeds) > 5800:
            raise ValueError('新闻单次投递超过 Discord 总容量')
        return Edition(embeds, publishable)

    async def preview(self, subscription, period):
        lock = self.locks.setdefault(subscription.id, asyncio.Lock())
        if lock.locked():
            return None
        async with lock:
            return await self.build(subscription, period, preview=True)

    async def publish(self, subscription, period, channel, *, retry_policy=None, view_factory=None):
        """`view_factory(run_id, item_count)` builds optional message components (feedback buttons).

        It runs after the send intent is persisted; without it the send call is unchanged.
        """
        lock = self.locks.setdefault(subscription.id, asyncio.Lock())
        if lock.locked():
            return None
        run_id = None
        intent_written = False

        async def build():
            nonlocal run_id
            if run_id is None:
                run_id = self.store.claim(subscription.id, period, channel.id)
                if run_id is None:
                    return None
            return await self.build(subscription, period)

        async def deliver(edition):
            nonlocal intent_written
            self.store.intent(run_id, edition.selected)
            intent_written = True
            view = None
            if view_factory is not None:
                try:
                    view = view_factory(run_id, len(edition.selected))
                except Exception as error:  # noqa: BLE001 - buttons are optional, the send is not
                    logger.warning('新闻按钮视图构造失败，改为无按钮发送 [%s]: %s', subscription.id, error)
                    view = None
            # No automatic retry of this operation, even for timeouts or cancellation.
            if view is None:
                message = await channel.send(embeds=edition.embeds)
            else:
                message = await channel.send(embeds=edition.embeds, view=view)
            if type(message.id) is not int or message.id <= 0:
                raise RuntimeError('Discord 未返回有效消息 ID')
            self.store.complete(run_id, message.id)

        try:
            result = await run_delivery_job(lock=lock, task_name=f'新闻 {subscription.id}',
                build=build, deliver=deliver,
                retry_policy=retry_policy or RetryPolicy(attempts=2, initial_delay_seconds=2))
            if result is None and run_id is not None:
                self.store.set_status(run_id, 'empty')
            return result
        except BaseException:
            if run_id is not None:
                # If even this write fails, persisted 'sending' is recovered as uncertain on restart.
                try:
                    self.store.set_status(run_id, 'uncertain' if intent_written else 'failed')
                except Exception:
                    logger.exception('新闻运行状态写入失败；重启后必须核查发送意图')
            raise

    async def publish_due(self, subscriptions, now, channel_lookup, *, views=None):
        """`views(subscription)` returns that subscription's `view_factory` (or None)."""
        from core.news.subscriptions import period_for

        async def run(subscription, period):
            try:
                channel = channel_lookup(subscription.channel_id)
                if channel is not None:
                    factory = views(subscription) if views is not None else None
                    await self.publish(subscription, period, channel, view_factory=factory)
            except Exception:
                logger.exception('新闻订阅执行失败: %s', subscription.id)

        # One failed/slow topic does not cancel siblings; model calls still share a semaphore.
        await asyncio.gather(*(run(sub, period) for sub in subscriptions if sub.enabled and sub.channel_id
                               and (period := period_for(sub, now, scheduled=True))))
