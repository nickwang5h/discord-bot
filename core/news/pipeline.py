"""One bounded path for every topic, with persistent send intent and per-subscription locks."""
import asyncio
import dataclasses
import datetime
import json
import logging

from core import ai_client
from core.claude_budget import estimate_input_tokens
from core.jobs import RetryPolicy, run_delivery_job
from core.news.ingest import Ingester
from core.news.models import Edition, fingerprint, trim_candidates
from core.news.sources import sources_for
from core.news.subscriptions import bounded_int, edition_name
from core.news.topics import TOPICS

logger = logging.getLogger(__name__)
MAX_INPUT_CHARS = 60000
MAX_RESULT_CHARS = 24000
# Sized for the smallest free provider (Groq qwen: 7000 input tokens per minute) so every
# topic can be selected without Claude. Override with NEWS_LIMITS.free_chain_input_tokens.
FREE_CHAIN_INPUT_TOKEN_LIMIT = 6500
# Candidates outrank history: shrink candidates to this floor first, then history.
MIN_FIT_CANDIDATES = 12
MIN_FIT_HISTORY = 10
# Personal topics (following) keep about this many candidates before history yields, then
# shorten excerpts (not below MIN_FIT_EXCERPT_CHARS) before dropping below it.
PERSONAL_FIT_CANDIDATES = 25
MIN_FIT_EXCERPT_CHARS = 300
HISTORY_KEYS = ('recently_delivered', 'already_recommended')
# With at least this many candidates an empty selection is treated as a soft failure: the
# provider chain tries the next provider once and accepts empty only if that is empty too.
MIN_CANDIDATES_FOR_EMPTY_RETRY = 10


def is_empty_selection(text):
    """True for a well-formed reply that selects nothing, e.g. {"items": []}."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get('items') == []


def empty_selection_check(candidate_count, label='新闻'):
    """The `soft_reject` predicate for generate_ai, or None when few candidates make empty plausible."""
    if candidate_count < MIN_CANDIDATES_FOR_EMPTY_RETRY:
        return None

    def reject(text):
        if is_empty_selection(text):
            logger.warning('%s 模型在 %d 个候选中返回空选择，视为软失败', label, candidate_count)
            return True
        return False
    return reject


def estimate_tokens(prepared):
    """Conservative system + user estimate (CJK 1.5 token/char, other 1 token/3 chars)."""
    raw = json.dumps(prepared.data, ensure_ascii=False, separators=(',', ':'))
    return estimate_input_tokens(prepared.system, raw)


def _largest_fit(low, high, build, limit):
    """Largest n in [low, high] whose build(n) fits (the estimate grows with n), else None."""
    best = None
    while low <= high:
        middle = (low + high) // 2
        candidate = build(middle)
        if estimate_tokens(candidate) <= limit:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def trim_history(prepared, count):
    """Keep the newest `count` history entries (topics list them newest first)."""
    data = dict(prepared.data)
    for key in HISTORY_KEYS:
        if isinstance(data.get(key), list):
            data[key] = data[key][:count]
    return dataclasses.replace(prepared, data=data)


def history_size(prepared):
    return max((len(prepared.data.get(key) or []) for key in HISTORY_KEYS), default=0)


def excerpt_size(prepared):
    """Longest candidate `content` excerpt the model sees (0 when candidates have none)."""
    return max((len(c['content']) for c in prepared.data.get('candidates') or []
                if isinstance(c, dict) and isinstance(c.get('content'), str)), default=0)


def shorten_excerpts(prepared, chars):
    """Cut each candidate's model-facing `content` to `chars`; bound evidence stays whole."""
    data = dict(prepared.data)
    data['candidates'] = [
        {**c, 'content': c['content'][:chars]}
        if isinstance(c, dict) and isinstance(c.get('content'), str) and len(c['content']) > chars else c
        for c in prepared.data.get('candidates') or []]
    return dataclasses.replace(prepared, data=data)


def fit_token_budget(prepared, limit, rebuild=None, *, minimum=MIN_FIT_CANDIDATES,
                     history_minimum=MIN_FIT_HISTORY, preferred=None, excerpt_minimum=None):
    """Fit the free-chain input budget by dropping whole candidates and history entries.

    `rebuild(n)` re-prepares with at most n candidates (personal topics re-rank at the
    smaller size); without it the prepared order is cut. Each step stops at the largest
    slate that fits:

    1. Drop candidates from the tail, but not below `preferred` (default `minimum`).
    2. Drop the oldest history entries, down to `history_minimum`.
    3. With `excerpt_minimum`, shorten the model-facing candidate excerpts, not below it.
    4. Drop candidates from `preferred` down to `minimum` (history and excerpts at their floors).

    Shared topics use the defaults (candidates to the floor, then history; no excerpt cuts).
    Whatever remains at the floors is sent; larger providers may still answer.
    """
    if estimate_tokens(prepared) <= limit:
        return prepared
    rebuild = rebuild or (lambda count: trim_candidates(prepared, count))
    total = len(prepared.candidates)
    keep = min(total, max(minimum, preferred or minimum))
    # 1. Candidates down to `keep`.
    if total > keep:
        best = _largest_fit(keep, total - 1, rebuild, limit)
        if best is not None:
            return best
        base = rebuild(keep)
    else:
        base = prepared
    # 2. History down to its floor.
    history = history_size(base)
    if history > history_minimum:
        best = _largest_fit(history_minimum, history - 1, lambda count: trim_history(base, count), limit)
        if best is not None:
            return best
        history = history_minimum
    excerpt = None

    def make(slate):
        slate = trim_history(slate, history)
        return slate if excerpt is None else shorten_excerpts(slate, excerpt)

    # 3. Shorter excerpts, not below `excerpt_minimum`.
    if excerpt_minimum is not None:
        longest = excerpt_size(base)
        if longest > excerpt_minimum:
            trimmed = trim_history(base, history)
            best = _largest_fit(excerpt_minimum, longest - 1, lambda chars: shorten_excerpts(trimmed, chars), limit)
            if best is not None:
                return best
            excerpt = excerpt_minimum
    # 4. Candidates below `keep`, down to `minimum`.
    floor = min(total, minimum)
    if keep > floor:
        best = _largest_fit(floor, keep - 1, lambda count: make(rebuild(count)), limit)
        if best is not None:
            return best
        return make(rebuild(floor))
    return make(base)


class NewsPipeline:
    def __init__(self, store, *, topics=None, limits=None, profile_provider=None):
        self.store = store
        # `profile_provider(subscription)` -> reader profile or None; personal topics only.
        self.profile_provider = profile_provider
        self.topics = TOPICS if topics is None else topics
        self.ingester = Ingester(store)
        self.locks = {}
        limits = {} if limits is None else limits
        if not isinstance(limits, dict) or set(limits) - {'concurrency', 'daily_calls', 'daily_output_tokens',
                                                          'free_chain_input_tokens'}:
            raise ValueError('NEWS_LIMITS 配置无效')
        self.model_slots = asyncio.Semaphore(bounded_int(limits.get('concurrency', 2), 1, 2))
        self.max_calls = bounded_int(limits.get('daily_calls', 24), 1, 48)
        self.max_tokens = bounded_int(limits.get('daily_output_tokens', 72000), 3000, 144000)
        self.input_tokens = bounded_int(limits.get('free_chain_input_tokens', FREE_CHAIN_INPUT_TOKEN_LIMIT),
                                        2000, 30000)

    async def build(self, subscription, period, *, preview=False):
        topic = self.topics[subscription.topic]
        topic.validate_params(subscription.params)
        await self.ingester.collect()
        history = [] if preview else self.store.history(subscription.id)
        articles = self.store.articles({s.name for s in sources_for(subscription.source_groups)}, age=topic.max_age)
        if not preview:
            articles = [a for a in articles if not self.store.seen(subscription.id, {'url': a.url, '_version': a.version})]
        personal = getattr(topic, 'personal', False)
        profile = await self._profile(subscription) if personal else None

        def prepare(scope):
            if personal:
                return topic.prepare(articles, scope, history, edition_name(period), profile=profile)
            return topic.prepare(articles, scope, history, edition_name(period))

        prepared = prepare(subscription)
        if not prepared.candidates:
            return None
        before, history_before, tokens = len(prepared.candidates), history_size(prepared), estimate_tokens(prepared)
        excerpt_before = excerpt_size(prepared)
        if personal:
            # Personal ranking reserves board/exploration slots, so it re-ranks at the smaller size;
            # history and excerpts yield before the slate drops below PERSONAL_FIT_CANDIDATES.
            prepared = fit_token_budget(
                prepared, self.input_tokens,
                lambda count: prepare(dataclasses.replace(subscription, max_candidates=count)),
                preferred=PERSONAL_FIT_CANDIDATES, excerpt_minimum=MIN_FIT_EXCERPT_CHARS)
        else:
            prepared = fit_token_budget(prepared, self.input_tokens)
        if tokens > self.input_tokens:
            logger.info('新闻 %s 输入约 %d token 超过 %d：候选 %d → %d，历史 %d → %d，摘录 ≤%d → ≤%d 字符（约 %d token）',
                        subscription.id, tokens, self.input_tokens, before, len(prepared.candidates),
                        history_before, history_size(prepared), excerpt_before, excerpt_size(prepared),
                        estimate_tokens(prepared))
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
                # Personal topics may use Claude; shared topics keep the free chain, call unchanged.
                routing = {'route': topic.route, 'json_schema': topic.json_schema} if personal else {}
                async with asyncio.timeout(180):
                    result = await ai_client.generate_ai(raw, system=prepared.system, use_search=False,
                        json_mode=True, max_output_tokens=subscription.max_output_tokens,
                        soft_reject=empty_selection_check(len(prepared.candidates), f'新闻 {subscription.id}'), **routing)
                text, attribution = result.text, result.attribution
        if not isinstance(text, str) or len(text) > MAX_RESULT_CHARS:
            raise ValueError('新闻模型结果超过容量')
        if personal:
            selected = topic.validate(text, prepared.candidates, subscription, history, profile=profile)
        else:
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

    async def _profile(self, subscription):
        if self.profile_provider is None:
            return None
        try:
            return await asyncio.to_thread(self.profile_provider, subscription)
        except Exception as error:  # noqa: BLE001 - selection falls back to no profile
            logger.warning('读者画像读取失败，本次不使用画像 [%s]: %s', subscription.id, error)
            return None

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
