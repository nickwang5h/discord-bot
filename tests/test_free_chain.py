"""Free-chain robustness: input token budget, lenient output parsing, Groq minute ledger."""
import datetime
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import ai_client
from core.ai_providers import AIResult, ProviderError
from core.claude_budget import estimate_input_tokens
from core.feeds import FeedItem
from core.news.ingest import normalize
from core.news.models import Article, SelectionInput, Subscription, lenient_items, public_history
from core.news.pipeline import (
    FREE_CHAIN_INPUT_TOKEN_LIMIT,
    MIN_FIT_CANDIDATES,
    MIN_FIT_HISTORY,
    MIN_CANDIDATES_FOR_EMPTY_RETRY,
    NewsPipeline,
    empty_selection_check,
    estimate_tokens,
    fit_token_budget,
    is_empty_selection,
)
from core.news.topics import TOPICS
from core.news.topics.discovery import (
    MAX_PERSONAL_INPUT_CHARS,
    _normalize_recommendations,
    _prepare_candidates,
    _render_recommendations,
)
from core.news.topics.general import _build_candidates, _normalize_selection
from core.news.topics.power_projects import POWER, PowerProjectsTopic

NOW = time.time()


def prepared(count, *, size=300):
    candidates = [{'id': f'R{i + 1:02}', 'title': f'Story {i}', 'content': 'x' * size} for i in range(count)]
    return SelectionInput(candidates, {'edition': '早间', 'candidates': [dict(c) for c in candidates]}, '系统提示')


def article(source, index, *, section='General', size=600):
    url = f'https://{source.lower()}.example/{index}'
    title = f'{source} headline number {index}'
    body = f'{title}. ' + 'Concrete detail with a figure. ' * (size // 30)
    return Article(url, source, url, title, body, section, NOW - 60 - index, NOW - 60 - index, f'v-{url}')


class TokenBudgetTests(unittest.TestCase):
    def test_estimate_counts_system_and_cjk_conservatively(self):
        self.assertEqual(estimate_input_tokens('中文', 'abcdef'), 5)  # 2 × 1.5 + 6 / 3
        slate = prepared(3)
        raw = json.dumps(slate.data, ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(estimate_tokens(slate), estimate_input_tokens('系统提示', raw))

    def test_under_budget_is_unchanged(self):
        slate = prepared(10)
        self.assertIs(fit_token_budget(slate, estimate_tokens(slate)), slate)

    def test_trims_from_tail_to_the_largest_count_that_fits(self):
        slate = prepared(30)
        for keep in (29, 17, MIN_FIT_CANDIDATES):
            with self.subTest(keep=keep):
                limit = estimate_tokens(prepared(keep))  # exactly fits `keep`, not keep + 1
                fitted = fit_token_budget(slate, limit)
                self.assertEqual([c['id'] for c in fitted.candidates], [f'R{i + 1:02}' for i in range(keep)])
                self.assertEqual(fitted.data['candidates'], slate.data['candidates'][:keep])
                self.assertEqual(fitted.data['edition'], '早间')
                self.assertLessEqual(estimate_tokens(fitted), limit)
                self.assertGreater(estimate_tokens(prepared(keep + 1)), limit)
        # Candidates are dropped whole, never cut.
        self.assertEqual(fitted.candidates[0]['content'], 'x' * 300)

    def test_nothing_fits_keeps_the_minimum(self):
        fitted = fit_token_budget(prepared(30), 10)
        self.assertEqual(len(fitted.candidates), MIN_FIT_CANDIDATES)
        self.assertEqual(len(fit_token_budget(prepared(3), 10).candidates), 3)

    def test_rebuild_is_used_for_re_ranked_topics(self):
        calls = []

        def rebuild(count):
            calls.append(count)
            return prepared(count)

        fitted = fit_token_budget(prepared(40), estimate_tokens(prepared(12)), rebuild)
        self.assertEqual(len(fitted.candidates), 12)
        self.assertTrue(calls)
        self.assertLessEqual(len(calls), 6)  # bisection, not one prepare per candidate

    def test_history_is_compacted_to_the_newest_thirty_titles(self):
        history = [{'title': f'Story {i}', 'rss_summary': f'  Summary {i} ' + 'x' * 300, 'content': 'long ' * 200,
                    'publisher': 'BBC', 'url': f'https://e.example/{i}', 'delivered_at': float(i),
                    'topic_result': {'project': 'P', 'digest_summary': '模型摘要'}} for i in range(35)]
        history.append({'title': 'Only title', 'delivered_at': 100.0})
        history.append({'title': '', 'rss_summary': 'no title', 'delivered_at': 101.0})
        compact = public_history(history)
        self.assertEqual(len(compact), 30)  # newest 30 with a title
        self.assertEqual(compact[0], {'title': 'Only title'})
        self.assertEqual(compact[1]['title'], 'Story 34')
        self.assertEqual(compact[-1]['title'], 'Story 6')
        self.assertTrue(all(set(entry) <= {'title', 'summary'} for entry in compact))
        self.assertTrue(compact[1]['summary'].startswith('Summary 34 '))
        self.assertEqual(len(compact[1]['summary']), 100)
        self.assertEqual(public_history([{'title': 'T', 'content': 'body', 'delivered_at': 1}]),
                         [{'title': 'T', 'summary': 'body'}])

    def test_every_topic_sends_the_compact_history(self):
        history = [{'title': f'Old {i}', 'rss_summary': 'r' * 400, 'content': 'c' * 900, 'publisher': 'BBC',
                    'category': 'World', 'url': f'https://old.example/{i}', 'delivered_at': NOW - i,
                    'evidence_hash': 'h', 'topic_result': {}} for i in range(40)]
        articles = [article('Src', i) for i in range(3)]
        for name, key in [('general', 'recently_delivered'), ('discovery', 'already_recommended'),
                          ('following', 'already_recommended'), ('power_projects', 'recently_delivered')]:
            with self.subTest(topic=name):
                topic = TOPICS[name]
                sub = Subscription(name, name, ('general',), ('08:00',), 1,
                                   {'countries': ['CA']} if name == 'power_projects' else {})
                slate = topic.prepare(articles, sub, history, '早间')
                self.assertEqual(len(slate.data[key]), 30)
                self.assertEqual(slate.data[key][0], {'title': 'Old 0', 'summary': 'r' * 100})

    def test_history_is_cut_only_after_candidates_reach_the_floor(self):
        def slate(candidates, history):
            base = prepared(candidates)
            base.data['recently_delivered'] = [{'title': f'Old {i}', 'summary': 'y' * 100} for i in range(history)]
            return base

        # Candidates first: a budget that fits 20 candidates with full history keeps all history.
        fitted = fit_token_budget(slate(30, 30), estimate_tokens(slate(20, 30)))
        self.assertEqual((len(fitted.candidates), len(fitted.data['recently_delivered'])), (20, 30))
        # At the candidate floor, history shrinks from the oldest end instead.
        fitted = fit_token_budget(slate(30, 30), estimate_tokens(slate(MIN_FIT_CANDIDATES, 18)))
        self.assertEqual(len(fitted.candidates), MIN_FIT_CANDIDATES)
        self.assertEqual(fitted.data['recently_delivered'], slate(1, 18).data['recently_delivered'])
        # Nothing fits: keep the candidate floor and the history floor.
        fitted = fit_token_budget(slate(30, 30), 10)
        self.assertEqual((len(fitted.candidates), len(fitted.data['recently_delivered'])),
                         (MIN_FIT_CANDIDATES, MIN_FIT_HISTORY))
        # Few candidates: none are dropped, history still yields.
        fitted = fit_token_budget(slate(8, 30), estimate_tokens(slate(8, 14)))
        self.assertEqual((len(fitted.candidates), len(fitted.data['recently_delivered'])), (8, 14))

    def test_limit_is_configurable_and_bounded(self):
        store = SimpleNamespace()
        self.assertEqual(NewsPipeline(store).input_tokens, FREE_CHAIN_INPUT_TOKEN_LIMIT)
        self.assertEqual(NewsPipeline(store, limits={'free_chain_input_tokens': 12000}).input_tokens, 12000)
        for bad in (100, 50000, '6500'):
            with self.assertRaises(ValueError):
                NewsPipeline(store, limits={'free_chain_input_tokens': bad})

    def test_following_keeps_char_cap_and_token_cap(self):
        articles = [article(f'Src{s}', i, size=900) for s in range(4) for i in range(15)]
        sub = Subscription('following', 'following', ('following',), ('08:20',), 2)
        topic = TOPICS['following']
        slate = topic.prepare(articles, sub, [], '早间')
        self.assertLessEqual(len(json.dumps(slate.data, ensure_ascii=False, separators=(',', ':'))),
                             MAX_PERSONAL_INPUT_CHARS)
        self.assertGreater(estimate_tokens(slate), FREE_CHAIN_INPUT_TOKEN_LIMIT)

        def rebuild(count):
            from dataclasses import replace
            return topic.prepare(articles, replace(sub, max_candidates=count), [], '早间')

        fitted = fit_token_budget(slate, FREE_CHAIN_INPUT_TOKEN_LIMIT, rebuild)
        self.assertLessEqual(estimate_tokens(fitted), FREE_CHAIN_INPUT_TOKEN_LIMIT)
        self.assertGreater(estimate_tokens(rebuild(len(fitted.candidates) + 1)), FREE_CHAIN_INPUT_TOKEN_LIMIT)
        self.assertEqual([c['id'] for c in fitted.candidates],
                         [f'R{i + 1:02}' for i in range(len(fitted.candidates))])


class PipelineBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_receives_the_trimmed_slate(self):
        import tempfile
        from pathlib import Path

        from core.news.store import NewsStore
        with tempfile.TemporaryDirectory() as tmp:
            store = NewsStore(Path(tmp) / 'news.sqlite3')
            try:
                store.import_history([])
                store.upsert_articles([normalize(FeedItem('World', f'Pub{i % 8}', f'Story {i}', f'https://example.com/{i}',
                                                 'Concrete observation with numbers. ' * 14, time.time() - i),
                                        time.time()) for i in range(40)])
                pipeline = NewsPipeline(store, limits={'free_chain_input_tokens': 4000})
                pipeline.ingester.collect = AsyncMock()
                seen = {}

                async def generate(raw, **kwargs):
                    seen['tokens'] = estimate_input_tokens(kwargs['system'], raw)
                    seen['count'] = len(json.loads(raw)['candidates'])
                    return AIResult('{"items":[]}', 'Test', 'model')

                sub = Subscription('discovery', 'discovery', ('discovery',), ('08:00',), 123)
                with patch('core.news.pipeline.ai_client.generate_ai', AsyncMock(side_effect=generate)), \
                        patch('core.news.pipeline.sources_for', return_value=[
                            SimpleNamespace(name=f'Pub{i}') for i in range(8)]):
                    self.assertIsNone(await pipeline.build(sub, '2026-10-18/08:00', preview=True))
                self.assertLessEqual(seen['tokens'], 4000)
                self.assertLess(seen['count'], 40)
                self.assertGreaterEqual(seen['count'], MIN_FIT_CANDIDATES)
            finally:
                store.close()


def recommendation(identity='R01', **extra):
    return {'id': identity, 'title': '中文标题', 'summary': '原文给出具体数字。', 'why_read': '理由具体。', **extra}


class LenientParsingTests(unittest.TestCase):
    def test_helper_rules(self):
        text = json.dumps({'items': [{'id': 'A', 'title': 't', 'extra': 1}, {'id': 'B', 'title': 'u', 'note': ''}],
                           'comment': 'ignored'})
        self.assertEqual(lenient_items(text, label='X', max_items=5, required=('id', 'title'),
                                       optional={'note': '', 'tags': []}),
                         [{'id': 'A', 'title': 't', 'note': '', 'tags': []},
                          {'id': 'B', 'title': 'u', 'note': '', 'tags': []}])
        self.assertEqual(len(lenient_items(text, label='X', max_items=1, required=('id',))), 1)
        for bad in ['{"items": "bad"}', '[]', '{"other": []}', '{"items": [1]}', '{"items": [{"title": "t"}]}']:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'X'):
                lenient_items(bad, label='X', max_items=5, required=('id', 'title'))

    def test_following_extra_missing_optional_and_overflow(self):
        candidates = _prepare_candidates([
            {'title': f'Story {i}', 'url': f'https://example.com/{i}', 'content': 'Concrete evidence.',
             'publisher': f'P{i}', 'source': 'General', 'published_at': NOW} for i in range(8)])
        items = [recommendation(f'R{i + 1:02}', tags=['芯片'], score=9) for i in range(7)]
        items[0].pop('tags')
        items[1].pop('why_read')
        items[2]['tags'] = '芯片'
        selected = _normalize_recommendations(json.dumps({'items': items}, ensure_ascii=False), candidates,
                                              with_tags=True)
        self.assertEqual([s['id'] for s in selected], ['R01', 'R02', 'R03', 'R04', 'R05'])  # model order, cut at 5
        self.assertEqual(selected[0]['tags'], [])
        self.assertEqual(selected[1]['why_read'], '')
        self.assertEqual(selected[2]['tags'], [])
        self.assertNotIn('score', selected[3])
        body = _render_recommendations(selected, numbered=True)
        self.assertEqual(body.count('值得一读'), 4)  # the line is omitted for the missing reason

    def test_core_fields_ids_and_text_rules_stay_strict(self):
        candidates = _prepare_candidates([{'title': 'Story', 'url': 'https://example.com/1',
                                           'content': 'Evidence.', 'publisher': 'P', 'published_at': NOW}])
        for broken in [{k: v for k, v in recommendation().items() if k != 'summary'},
                       {k: v for k, v in recommendation().items() if k != 'id'},
                       recommendation('R99'), recommendation(why_read='https://invented.example'),
                       recommendation(why_read='only english'), recommendation(summary='')]:
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                _normalize_recommendations(json.dumps({'items': [broken]}, ensure_ascii=False), candidates)
        with self.assertRaisesRegex(ValueError, '重复'):
            _normalize_recommendations(json.dumps({'items': [recommendation(), recommendation()]},
                                                  ensure_ascii=False), candidates)

    def test_general_ignores_extra_fields_and_cuts_overflow(self):
        candidates = _build_candidates([FeedItem('World', f'P{i}', f'Story {i}', f'https://example.com/{i}',
                                                 'Evidence.', NOW) for i in range(2)])
        items = [{'id': 'N01', 'title': '中文标题', 'summary': '具体事实。', 'url': 'https://invented.example',
                  'category': 'Tech'},
                 {'id': 'N02', 'title': '中文标题二', 'summary': '具体事实二。'},
                 {'id': 'N01', 'title': '重复', 'summary': '重复。'}]
        selected = _normalize_selection(json.dumps({'items': items}, ensure_ascii=False), candidates)
        self.assertEqual([s['id'] for s in selected], ['N01', 'N02'])
        self.assertEqual(selected[0]['url'], 'https://example.com/0')  # links stay bound to the candidate
        self.assertEqual(selected[0]['category'], 'World')
        with self.assertRaises(ValueError):
            _normalize_selection(json.dumps({'items': [{'id': 'N01', 'title': '中文标题'}]}), candidates)


class PowerLeniencyTests(unittest.TestCase):
    def setUp(self):
        date = datetime.datetime.now(datetime.UTC).date().isoformat()
        text = f'Canada: North Grid approved the Maple Project substation in Ottawa on {date}.'
        self.topic = PowerProjectsTopic()
        self.sub = Subscription('power', 'power_projects', ('general',), ('12:00',), 123, {'countries': ['CA']})
        self.candidates = self.topic.prepare([normalize(FeedItem('World', 'BBC World', 'Canada power update',
                                                                 'https://example.com/p', text, time.time()),
                                                        time.time())], self.sub, [], '午后').candidates
        self.item = {'id': 'P01', 'title': '加拿大电网项目获批', 'summary': '该变电站项目获批。',
                     'project': 'Maple Project', 'country': 'CA', 'country_quote': 'Canada', 'stage': 'planning',
                     'stage_quote': 'approved', 'new_fact': 'North Grid approved the Maple Project substation'}

    def test_missing_optional_fields_become_null_and_extra_fields_are_ignored(self):
        selected = self.topic.validate(json.dumps({'items': [{**self.item, 'confidence': 0.9, 'actor': ''}]},
                                                  ensure_ascii=False), self.candidates, self.sub, [])
        self.assertEqual([selected[0][k] for k in ('actor', 'location', 'equipment', 'event_date')], [None] * 4)
        self.assertNotIn('confidence', selected[0])
        self.assertIn('原文未提供', self.topic.render(selected, '午后', 'Test')[0].description)

    def test_core_and_quoted_fields_stay_strict_and_overflow_is_cut(self):
        for update in [{'project': None}, {'actor': 'Imaginary buyer'}, {'new_fact': 'invented'}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.topic.validate(json.dumps({'items': [{**self.item, **update}]}), self.candidates, self.sub, [])
        missing = {k: v for k, v in self.item.items() if k != 'stage_quote'}
        with self.assertRaisesRegex(ValueError, '字段无效'):
            self.topic.validate(json.dumps({'items': [missing]}), self.candidates, self.sub, [])
        # Overflow is cut in model order before any check: the garbage tail is never seen.
        with patch('core.news.topics.power_projects.MAX_ITEMS', 1):
            selected = self.topic.validate(json.dumps({'items': [self.item, {'x': 1}]}), self.candidates,
                                           self.sub, [])
        self.assertEqual(len(selected), 1)
        with self.assertRaises(ValueError):
            self.topic.validate(json.dumps({'items': [self.item, {'x': 1}]}), self.candidates, self.sub, [])

    def test_power_filter_accepts_plurals(self):
        for text in ('new substations', 'regional grids', 'large transformers'):
            self.assertTrue(POWER.search(text), text)


class GroqLedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_client._groq_input_window.clear()

    def tearDown(self):
        ai_client._groq_input_window.clear()

    def test_rolling_minute_window(self):
        reserve = ai_client._reserve_groq_input
        self.assertTrue(reserve(9000, now=0.0))  # a lone request is always tried
        self.assertFalse(reserve(10, now=30.0))
        self.assertTrue(reserve(6500, now=60.0))  # the first one has aged out
        self.assertTrue(reserve(500, now=61.0))  # exactly at the limit
        self.assertFalse(reserve(1, now=62.0))

    async def test_second_large_request_in_a_minute_skips_groq(self):
        calls = []

        async def compatible(**kwargs):
            calls.append(kwargs['provider'])
            return AIResult('{"items":[]}', kwargs['provider'], 'm')

        text = 'x' * 15000  # about 5000 tokens
        with patch('core.ai_client.settings.get_secret', return_value='key'), \
                patch('core.ai_client.request_openai_compatible', AsyncMock(side_effect=compatible)):
            errors = []
            first = await ai_client._ask_compatible_providers(text, '', json_mode=True, max_output_tokens=10,
                                                              errors=errors)
            second = await ai_client._ask_compatible_providers(text, '', json_mode=True, max_output_tokens=10,
                                                               errors=errors)
        self.assertEqual((first.provider, second.provider), ('Groq', 'Zhipu'))
        self.assertEqual(calls, ['Groq', 'Zhipu'])
        self.assertIn('跳过 Groq', errors[0])

    async def test_groq_skip_raises_provider_error_without_a_request(self):
        ai_client._reserve_groq_input(7000)
        with patch('core.ai_client.settings.get_secret', return_value='key'), \
                patch('core.ai_client.request_openai_compatible', AsyncMock()) as request:
            with self.assertRaises(ProviderError):
                await ai_client._ask_groq('x' * 30, '')
        request.assert_not_called()

    def test_free_chain_timeouts_fit_inside_the_news_cap(self):
        total = (ai_client.GROQ_TIMEOUT_SECONDS + ai_client.ZHIPU_TIMEOUT_SECONDS
                 + ai_client.OPENROUTER_TIMEOUT_SECONDS + 30)  # Gemini offline
        self.assertLessEqual(total, 150)
        self.assertGreaterEqual(ai_client.OPENROUTER_TIMEOUT_SECONDS, 45)


class EmptySelectionRetryTests(unittest.IsolatedAsyncioTestCase):
    """An empty selection over many candidates is a soft failure: one retry on the next provider."""
    EMPTY = '{"items":[]}'
    FULL = '{"items":[{"id":"R01"}]}'

    def setUp(self):
        ai_client._groq_input_window.clear()
        self.calls = []

    def tearDown(self):
        ai_client._groq_input_window.clear()

    def chain(self, answers):
        """Patch the compatible providers; `answers[provider]` is text or an exception."""
        async def compatible(**kwargs):
            provider = kwargs['provider']
            self.calls.append(provider)
            answer = answers[provider]
            if isinstance(answer, Exception):
                raise answer
            return AIResult(answer, provider, 'm')
        return compatible

    async def run_chain(self, answers, reject, *, gemini=None):
        patches = [patch('core.ai_client.settings.get_secret', return_value='key'),
                   patch('core.ai_client.request_openai_compatible', AsyncMock(side_effect=self.chain(answers))),
                   patch('core.ai_client.model_available', gemini is not None),
                   patch('core.ai_client.client', object() if gemini is not None else None),
                   patch('core.ai_client._ask_gemini', AsyncMock(side_effect=gemini or ProviderError('off')))]
        for item in patches:
            item.start()
        try:
            return await ai_client.generate_ai('x', system='s', json_mode=True, soft_reject=reject)
        finally:
            for item in reversed(patches):
                item.stop()

    def test_predicate(self):
        self.assertTrue(is_empty_selection(self.EMPTY))
        self.assertTrue(is_empty_selection('{"items": [], "note": "none"}'))
        for text in (self.FULL, '{}', '[]', 'not json', '{"items": null}', None):
            self.assertFalse(is_empty_selection(text))
        self.assertIsNone(empty_selection_check(MIN_CANDIDATES_FOR_EMPTY_RETRY - 1))
        reject = empty_selection_check(MIN_CANDIDATES_FOR_EMPTY_RETRY)
        self.assertTrue(reject(self.EMPTY))
        self.assertFalse(reject(self.FULL))
        self.assertEqual(MIN_CANDIDATES_FOR_EMPTY_RETRY, 10)

    async def test_empty_falls_through_to_the_next_provider(self):
        with self.assertLogs('core.ai_client', 'WARNING') as logs:
            result = await self.run_chain({'Groq': self.EMPTY, 'Zhipu': self.FULL}, empty_selection_check(33))
        self.assertEqual((result.provider, result.text), ('Zhipu', self.FULL))
        self.assertEqual(self.calls, ['Groq', 'Zhipu'])
        self.assertTrue(any('软失败' in line for line in logs.output))

    async def test_empty_twice_is_accepted_without_a_third_call(self):
        result = await self.run_chain({'Groq': self.EMPTY, 'Zhipu': self.EMPTY, 'OpenRouter': self.FULL},
                                      empty_selection_check(21))
        self.assertEqual((result.provider, result.text), ('Zhipu', self.EMPTY))
        self.assertEqual(self.calls, ['Groq', 'Zhipu'])

    async def test_next_answering_provider_counts_after_hard_errors(self):
        result = await self.run_chain({'Groq': self.EMPTY, 'Zhipu': ProviderError('429'), 'OpenRouter': self.FULL},
                                      empty_selection_check(21))
        self.assertEqual(result.provider, 'OpenRouter')
        self.assertEqual(self.calls, ['Groq', 'Zhipu', 'OpenRouter'])

    async def test_held_empty_is_returned_when_no_later_provider_answers(self):
        result = await self.run_chain({'Groq': self.EMPTY, 'Zhipu': ProviderError('429'),
                                       'OpenRouter': ProviderError('timeout')}, empty_selection_check(21))
        self.assertEqual((result.provider, result.text), ('Groq', self.EMPTY))

    async def test_gemini_is_the_next_provider_at_the_end_of_the_chain(self):
        gemini = [AIResult(self.FULL, 'Gemini', 'g')]
        result = await self.run_chain({'Groq': ProviderError('x'), 'Zhipu': ProviderError('x'),
                                       'OpenRouter': self.EMPTY}, empty_selection_check(21), gemini=gemini)
        self.assertEqual(result.provider, 'Gemini')

    async def test_few_candidates_accept_empty_at_once(self):
        result = await self.run_chain({'Groq': self.EMPTY, 'Zhipu': self.FULL}, empty_selection_check(9))
        self.assertEqual(result.provider, 'Groq')
        self.assertEqual(self.calls, ['Groq'])
        result = await self.run_chain({'Groq': self.EMPTY, 'Zhipu': self.FULL}, None)
        self.assertEqual(result.provider, 'Groq')

    async def test_empty_claude_answer_retries_on_the_free_chain(self):
        claude = AsyncMock(return_value=AIResult(self.EMPTY, 'Claude', 'claude-opus-5-5'))
        with patch('core.ai_client._ask_claude', claude), \
                patch('core.ai_client.settings.get_secret', return_value='key'), \
                patch('core.ai_client.request_openai_compatible',
                      AsyncMock(side_effect=self.chain({'Groq': self.FULL}))):
            result = await ai_client.generate_ai('x', system='s', json_mode=True, route='personal.following',
                                                 json_schema={}, soft_reject=empty_selection_check(40))
        self.assertEqual(result.provider, 'Groq')
        claude.assert_awaited_once()

    async def test_pipeline_passes_the_check_only_with_enough_candidates(self):
        import tempfile
        from pathlib import Path

        from core.news.store import NewsStore
        for count, expect in ((30, True), (5, False)):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                store = NewsStore(Path(tmp) / 'news.sqlite3')
                try:
                    store.import_history([])
                    store.upsert_articles([normalize(FeedItem('World', f'Pub{i % 8}', f'Story {i}',
                                                              f'https://example.com/{i}', 'Concrete detail. ' * 5,
                                                              time.time() - i), time.time()) for i in range(count)])
                    pipeline = NewsPipeline(store)
                    pipeline.ingester.collect = AsyncMock()
                    model = AsyncMock(return_value=AIResult(self.EMPTY, 'Test', 'model'))
                    sub = Subscription('discovery', 'discovery', ('discovery',), ('08:00',), 123)
                    with patch('core.news.pipeline.ai_client.generate_ai', model), \
                            patch('core.news.pipeline.sources_for', return_value=[
                                SimpleNamespace(name=f'Pub{i}') for i in range(8)]):
                        self.assertIsNone(await pipeline.build(sub, '2026-10-19/08:00', preview=True))
                    reject = model.await_args.kwargs['soft_reject']
                    self.assertEqual(reject is not None, expect)
                    if expect:
                        self.assertTrue(reject(self.EMPTY))
                finally:
                    store.close()


if __name__ == '__main__':
    unittest.main()
