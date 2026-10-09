"""T6: reader profile flows back into the personal `following` selection (design §3)."""
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import news as news_module
from cogs.feedback import Feedback, current_weight
from cogs.news import News
from core.ai_providers import AIResult
from core.feedback.profile import DAY, W_MIN, build
from core.feedback.store import FeedbackStore
from core.jobs import RetryPolicy
from core.news.models import Article, Subscription
from core.news.pipeline import NewsPipeline
from core.news.reader import DeliveryReader
from core.news.store import NewsStore
from core.news.topics import TOPICS
from core.news.topics.discovery import (
    MAX_PERSONAL_INPUT_CHARS,
    FollowingTopic,
    _normalize_recommendations,
    _prepare_candidates,
    apply_profile,
    clean_tags,
)
from core.news.topics.ranking import _board_quotas, apportion, rank, smooth_round_robin

NOW = time.time()
FOLLOWING = Subscription('following', 'following', ('following',), ('08:20',), 2)
SECTIONS = {'ottawa': 'Ottawa', 'sudbury': 'Sudbury', 'invest': 'Investing', 'ai_tech': 'AI-Tech',
            'general': 'General'}


def fb(verdict, *, source='Seed', board='general', tags=(), n=1, days=0.0):
    return [{'key': f'{source}-{verdict}-{i}-{tags}', 'verdict': verdict, 'weight': 1.0, 'coarse': False,
             'via': 'button', 'updated_at': NOW - days * DAY, 'source': source, 'category': '',
             'board': board, 'tags': list(tags), 'personal': True} for i in range(n)]


def article(source, index, *, section='Investing', age=60.0, size=200):
    url = f'https://{source.replace(" ", "").lower()}.example/{index}'
    title = f'{source} headline number {index}'
    body = f'{title}. ' + 'Concrete detail with a figure. ' * max(1, size // 30)
    return Article(url, source, url, title, body, section, NOW - age - index, NOW - age - index, f'v-{url}')


def raw_items(articles):
    return [{'title': a.title, 'url': a.url, 'content': a.content, 'publisher': a.source,
             'source': a.category, 'published_at': a.published_at, 'timestamp': a.first_seen} for a in articles]


def explore_profile():
    """Hot1–Hot4 well liked (W ≈ 1.55), Dull pushed to the 0.25 floor, Fresh never rated."""
    data = fb('known', source='Dull', board='invest', n=30)
    for name in ('Hot1', 'Hot2', 'Hot3', 'Hot4'):
        data += fb('new', source=name, board='invest', n=10)
    return build(data, NOW)


def tag_profile():
    data = (fb('new', source='Seed', n=12, tags=['芯片'])
            + fb('known', source='Seed', n=6, tags=['轻轨'])
            + fb('skip', source='Seed', n=5, tags=['加息']))
    return build(data, NOW)


def item(identity, *, tags, summary='原文给出具体数字。'):
    return {'id': identity, 'title': '中文标题', 'summary': summary, 'why_read': '理由具体。', 'tags': tags}


class QuotaAndRoundRobinTests(unittest.TestCase):
    def test_largest_remainder_with_caps(self):
        self.assertEqual(apportion(10, {'a': 1, 'b': 1, 'c': 1}, {'a': 9, 'b': 9, 'c': 9}), {'a': 4, 'b': 3, 'c': 3})
        self.assertEqual(apportion(5, {'a': 3, 'b': 1}, {'a': 2, 'b': 9}), {'a': 2, 'b': 3})
        self.assertEqual(apportion(4, {'a': 1}, {'a': 2}), {'a': 2})

    def test_board_floor_then_weighted_remainder(self):
        # Floors 3/3/2; remaining 4 split by W×count = 20 : 10 : (capped) → 2.67 / 1.33 → 3 / 1.
        quotas = _board_quotas({'a': 10, 'b': 10, 'c': 2}, {'a': 2.0, 'b': 1.0, 'c': 1.0}, 12)
        self.assertEqual(quotas, {'a': 6, 'b': 4, 'c': 2})
        # Fewer slots than floors: one per board, heaviest first.
        self.assertEqual(_board_quotas({'a': 5, 'b': 5, 'c': 5}, {'a': 1.0, 'b': 3.0, 'c': 2.0}, 2),
                         {'a': 0, 'b': 1, 'c': 1})

    def test_smooth_weighted_round_robin_ratio(self):
        queues = {'A': [f'a{i}' for i in range(10)], 'B': [f'b{i}' for i in range(10)]}
        order = smooth_round_robin(queues, {'A': 2.0, 'B': 1.0}, 6)
        self.assertEqual(order, ['a0', 'b0', 'a1', 'a2', 'b1', 'a3'])
        self.assertEqual(smooth_round_robin({'A': ['a0'], 'B': ['b0', 'b1']}, {'A': 5.0, 'B': 1.0}, 9),
                         ['a0', 'b0', 'b1'])

    def test_every_board_keeps_its_floor_and_weights_shift_the_rest(self):
        liked = build(fb('new', source='x', board='ai_tech', n=15) + fb('known', source='y', board='ottawa', n=15),
                      NOW)
        candidates = [{'publisher': f'{board}-src', 'source': SECTIONS[board], 'url': f'{board}{i}'}
                      for board in ('ottawa', 'ai_tech', 'sudbury') for i in range(20)]
        picked = rank(candidates, liked, 20)
        count = {board: sum(c['url'].startswith(board) for c in picked) for board in ('ottawa', 'ai_tech', 'sudbury')}
        self.assertEqual(sum(count.values()), 20)
        self.assertGreaterEqual(min(count.values()), 3)
        self.assertGreater(count['ai_tech'], count['sudbury'])
        self.assertGreater(count['sudbury'], count['ottawa'])


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.profile = explore_profile()
        self.assertEqual(self.profile.weight_for_source('Dull'), W_MIN)
        self.assertIsNone(self.profile.feature('source', 'Fresh'))
        names = ('Hot1', 'Hot2', 'Hot3', 'Hot4', 'Dull', 'Fresh')
        self.candidates = [{'publisher': name, 'source': 'Investing', 'url': f'{name}-{i}'}
                           for i in range(15) for name in names]

    def test_cold_share_and_floor_slot_are_guaranteed(self):
        for limit in (5, 10, 20, 40):
            picked = rank(self.candidates, self.profile, limit)
            self.assertEqual(len(picked), limit)
            self.assertEqual(len({c['url'] for c in picked}), limit)
            fresh = sum(c['publisher'] == 'Fresh' for c in picked)
            self.assertGreaterEqual(fresh, math.ceil(0.2 * limit), limit)
            self.assertGreaterEqual(sum(c['publisher'] == 'Dull' for c in picked), 1, limit)

    def test_without_exploration_the_weights_alone_would_starve_them(self):
        queues = {}
        for c in self.candidates:
            queues.setdefault(c['publisher'], []).append(c)
        weights = {name: self.profile.weight_for_source(name) for name in queues}
        plain = smooth_round_robin(queues, weights, 20)
        self.assertLess(sum(c['publisher'] == 'Fresh' for c in plain), 4)

    def test_sources_keep_newest_first(self):
        picked = rank(self.candidates, self.profile, 20)
        for name in ('Hot1', 'Fresh'):
            indexes = [int(c['url'].split('-')[1]) for c in picked if c['publisher'] == name]
            self.assertEqual(indexes, sorted(indexes))


class PrepareTests(unittest.TestCase):
    def articles(self):
        return ([article('Alpha', i, section='Ottawa') for i in range(12)]
                + [article('Beta', i, section='AI-Tech') for i in range(5)]
                + [article('Gamma', i, section='Investing') for i in range(30)])

    def test_without_profile_order_matches_the_old_interleave(self):
        articles = self.articles()
        for cap in (40, 25):
            sub = Subscription('following', 'following', ('following',), ('08:20',), 2, max_candidates=cap)
            prepared = TOPICS['following'].prepare(articles, sub, [], '早间', profile=None)
            old = _prepare_candidates(raw_items(articles))[:cap]
            self.assertEqual([c['url'] for c in prepared.candidates], [c['url'] for c in old])
            self.assertEqual([c['id'] for c in prepared.candidates], [c['id'] for c in old])
            self.assertEqual(set(prepared.data), {'candidates', 'already_recommended'})

    def test_profile_goes_into_the_input_and_changes_the_cache_key_data(self):
        articles = self.articles()
        first = TOPICS['following'].prepare(articles, FOLLOWING, [], '早间', profile=tag_profile())
        self.assertEqual(set(first.data), {'candidates', 'already_recommended', 'reader_profile', 'tag_vocabulary'})
        self.assertIn('轻轨', first.data['reader_profile']['known_topics'])
        self.assertIn('加息', first.data['reader_profile']['not_interested'])
        self.assertIn('芯片', first.data['tag_vocabulary'])
        other = build(fb('new', source='Seed', n=15, tags=['电网']), NOW)
        second = TOPICS['following'].prepare(articles, FOLLOWING, [], '早间', profile=other)
        self.assertNotEqual(first.data['reader_profile'], second.data['reader_profile'])
        self.assertIn('reader_profile', FollowingTopic.system)
        self.assertIn('不可信数据', FollowingTopic.system)

    def test_input_is_bounded_for_the_claude_route(self):
        articles = [article(f'Src{s}', i, section='General', size=900) for s in range(4) for i in range(15)]
        prepared = TOPICS['following'].prepare(articles, FOLLOWING, [], '早间', profile=explore_profile())
        raw = json.dumps(prepared.data, ensure_ascii=False, separators=(',', ':'))
        self.assertLessEqual(len(raw), MAX_PERSONAL_INPUT_CHARS)
        self.assertGreater(len(prepared.candidates), 10)
        self.assertEqual([c['id'] for c in prepared.candidates],
                         [f'R{i + 1:02}' for i in range(len(prepared.candidates))])

    def test_shared_discovery_has_no_profile_parameter(self):
        self.assertFalse(TOPICS['discovery'].personal)
        with self.assertRaises(TypeError):
            TOPICS['discovery'].prepare([], FOLLOWING, [], '早间', profile=None)


class TagAndPostProcessTests(unittest.TestCase):
    def test_clean_tags(self):
        self.assertEqual(clean_tags(['  Nvidia ', 'ＯＰＧ', 'nvidia', '芯片', '第四个']), ['Nvidia', 'OPG', '芯片'])
        self.assertEqual(clean_tags(['x', '超过十二个字的一个很长的标签', 'https://a.b', '**粗体**', '[链接](u)', 3]), [])
        self.assertEqual(clean_tags([]), [])
        with self.assertRaises(ValueError):
            clean_tags('芯片')

    def test_following_output_accepts_tags_and_discovery_does_not(self):
        candidates = _prepare_candidates(raw_items([article('Alpha', 1)]))
        text = json.dumps({'items': [{**item('R01', tags=['芯片', 'http://x'])}]}, ensure_ascii=False)
        selected = _normalize_recommendations(text, candidates, with_tags=True)
        self.assertEqual(selected[0]['tags'], ['芯片'])
        with self.assertRaises(ValueError):
            _normalize_recommendations(text, candidates)
        untagged = json.dumps({'items': [{k: v for k, v in item('R01', tags=[]).items() if k != 'tags'}]})
        self.assertEqual(_normalize_recommendations(untagged, candidates, with_tags=True)[0]['tags'], [])
        extra = json.dumps({'items': [{**item('R01', tags=[]), 'score': 1}]})
        with self.assertRaises(ValueError):
            _normalize_recommendations(extra, candidates, with_tags=True)

    def test_strong_skip_dropped_known_moved_last(self):
        profile = tag_profile()
        selected = [item('R01', tags=['轻轨']), item('R02', tags=['加息']), item('R03', tags=['芯片']),
                    item('R04', tags=['轻轨'], summary='新进展：线路延期。'), item('R05', tags=[]),
                    item('R06', tags=['加息', '芯片'])]
        result = apply_profile(selected, profile)
        self.assertEqual([i['id'] for i in result], ['R03', 'R04', 'R05', 'R06', 'R01'])
        self.assertIs(apply_profile(selected, None), selected)


class FakeFeedback:
    def __init__(self, rows=None, error=None):
        self.store = SimpleNamespace(feedback_since=MagicMock(return_value=rows or [], side_effect=error))


class PipelineRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'news.sqlite3'
        self.store = NewsStore(self.path)
        self.store.import_history([])
        self.store.upsert_articles([article('BBC World', i, section='World') for i in range(3)])
        self.profile = tag_profile()
        self.provider = MagicMock(return_value=self.profile)
        self.pipeline = NewsPipeline(self.store, profile_provider=self.provider)
        self.pipeline.ingester.collect = AsyncMock()
        self.model = AsyncMock(side_effect=self.answer)
        self.patch = patch('core.news.pipeline.ai_client.generate_ai', self.model)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    async def answer(raw, **kwargs):
        ids = [c['id'] for c in json.loads(raw)['candidates']]
        if kwargs.get('route'):
            items = [item(ids[0], tags=['芯片', 'Nvidia']), item(ids[1], tags=['加息']), item(ids[2], tags=['轻轨'])]
            return AIResult(json.dumps({'items': items}, ensure_ascii=False), 'Claude', 'claude-opus-5-5')
        entry = {'id': ids[0], 'title': '有依据的中文标题', 'summary': '原文提供了新的具体观察。'}
        if ids[0].startswith('R'):
            entry['why_read'] = '可以想一想：条件是否改变结论？'
        payload = {'items': [entry]}
        return AIResult(json.dumps(payload, ensure_ascii=False), 'Test', 'model')

    def sub(self, identity, topic):
        return Subscription(identity, topic, ('general',), ('08:00',), 123)

    async def test_route_and_schema_only_for_the_personal_topic(self):
        await self.pipeline.preview(self.sub('general', 'general'), '2026-08-01/08:00')
        await self.pipeline.preview(self.sub('discovery', 'discovery'), '2026-08-01/08:00')
        edition = await self.pipeline.preview(self.sub('following', 'following'), '2026-08-01/08:00')
        shared = {'system', 'use_search', 'json_mode', 'max_output_tokens'}
        for call in self.model.await_args_list[:2]:
            # The shared call is exactly what it was before T6.
            self.assertEqual(set(call.kwargs), shared)
            self.assertEqual((call.kwargs['use_search'], call.kwargs['json_mode'], call.kwargs['max_output_tokens']),
                             (False, True, 3000))
        personal = self.model.await_args_list[2].kwargs
        self.assertEqual(set(personal), shared | {'route', 'json_schema'})
        self.assertEqual(personal['route'], 'personal.following')
        self.assertIs(personal['json_schema'], FollowingTopic.json_schema)
        self.assertTrue(personal['json_mode'])
        self.provider.assert_called_once()
        # 加息 is strongly not interesting (dropped); 轻轨 is known (moved last).
        self.assertEqual([i['tags'] for i in edition.selected], [['芯片', 'Nvidia'], ['轻轨']])

    async def test_profile_change_reselects_and_same_profile_hits_cache(self):
        sub = self.sub('following', 'following')
        await self.pipeline.preview(sub, '2026-08-01/08:00')
        await self.pipeline.preview(sub, '2026-08-01/08:00')
        self.assertEqual(self.model.await_count, 1)
        self.provider.return_value = build(fb('new', source='Seed', n=15, tags=['电网']), NOW)
        await self.pipeline.preview(sub, '2026-08-01/08:00')
        self.assertEqual(self.model.await_count, 2)

    async def test_provider_failure_or_absence_means_no_profile(self):
        self.provider.side_effect = RuntimeError('db locked')
        with self.assertLogs('core.news.pipeline', 'WARNING'):
            edition = await self.pipeline.preview(self.sub('following', 'following'), '2026-08-01/08:00')
        self.assertNotIn('reader_profile', json.loads(self.model.await_args.args[0]))
        self.assertEqual(len(edition.selected), 3)
        self.pipeline.profile_provider = None
        self.store.upsert_articles([article('BBC World', 9, section='World')])
        await self.pipeline.preview(self.sub('following', 'following'), '2026-08-02/08:00')
        self.assertNotIn('reader_profile', json.loads(self.model.await_args.args[0]))

    async def test_model_tags_reach_the_feedback_store(self):
        channel = SimpleNamespace(id=123, send=AsyncMock(return_value=SimpleNamespace(id=456)))
        await self.pipeline.publish(self.sub('following', 'following'), '2026-08-01/08:00', channel,
                                    retry_policy=RetryPolicy(attempts=1))
        feedback = FeedbackStore(Path(self.tmp.name) / 'feedback.sqlite3', now=0)
        try:
            feedback.sync_exposures(DeliveryReader(self.path), [FOLLOWING])
            items = feedback.items_for_ref('run', self.store.status('following')['latest']['id'])
            self.assertEqual([i['tags'] for i in items], [['芯片', 'Nvidia'], ['轻轨']])
            self.assertEqual([i['position'] for i in items], [0, 1])
        finally:
            feedback.close()


class CogWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.feedback = None
        self.bot.get_cog.side_effect = lambda name: self.feedback if name == 'Feedback' else None
        with patch.object(news_module, 'SCHEDULED_JOBS_ENABLED', False):
            self.cog = News(self.bot)

    async def test_reader_profile_from_feedback_store(self):
        self.assertIsNone(self.cog._reader_profile(FOLLOWING))
        rows = fb('new', source='Seed', n=15, tags=['芯片'])
        self.feedback = FakeFeedback(rows)
        profile = self.cog._reader_profile(FOLLOWING)
        self.assertEqual(profile.exact, 15)
        since = self.feedback.store.feedback_since.call_args.args[0]
        self.assertAlmostEqual(time.time() - since, 90 * DAY, delta=60)
        self.feedback = FakeFeedback(rows[:5])
        self.assertIsNone(self.cog._reader_profile(FOLLOWING))   # cold start
        self.feedback = FakeFeedback(error=RuntimeError('locked'))
        with self.assertLogs('cogs.news', 'WARNING'):
            self.assertIsNone(self.cog._reader_profile(FOLLOWING))

    async def test_pipeline_gets_the_provider(self):
        with patch.object(news_module, 'NewsStore'), patch.object(news_module, 'NewsPipeline') as pipeline:
            self.cog.pipeline
        self.assertEqual(pipeline.call_args.kwargs['profile_provider'], self.cog._reader_profile)
        self.cog._pipeline = None


class StatsTests(unittest.TestCase):
    def test_weight_column_and_profile_fragment(self):
        profile = explore_profile()
        blank = {'name': None, 'exposures': 3, 'rated': 3, 'exact': 3, 'new': 0.0, 'known': 3.0, 'skip': 0.0,
                 'new_rate': 0.0}
        result = {'totals': blank, 'rows': [{**blank, 'name': 'Dull'}, {**blank, 'name': 'Unknown'}]}
        text = Feedback.render_stats(result, 30, 'source', profile)
        self.assertIn('`Dull` · 曝光 3 · 已评 3 · 🆕0 👌3 🚫0 · 新知率 0% · 权重 0.25', text)
        self.assertIn('`Unknown` · 曝光 3', text)
        self.assertIn('权重 1.00', text)
        self.assertEqual(current_weight(None, 'tag', 'x'), 1.0)
        embed = Feedback.profile_embed(profile)
        self.assertIn(profile.prompt_fragment(), embed.description)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertIn('冷启动', Feedback.profile_embed(None).description)


if __name__ == '__main__':
    unittest.main()
