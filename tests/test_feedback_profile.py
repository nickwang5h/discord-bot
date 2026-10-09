import json
import random
import unittest
from fractions import Fraction

from core.feedback import profile
from core.feedback.profile import DAY, build, explain

NOW = 1_800_000_000.0


def row(verdict, *, days=0.0, source='S', board='general', tags=(), coarse=False, weight=1.0, personal=True, key=None):
    return {'key': key or f'k{random.random()}', 'verdict': verdict, 'weight': weight, 'coarse': coarse,
            'via': 'reaction' if coarse else 'button', 'updated_at': NOW - days * DAY, 'source': source,
            'category': 'World', 'board': board, 'tags': list(tags), 'personal': personal}


def rows(n, verdict, **kw):
    return [row(verdict, **kw) for _ in range(n)]


def expected_weight(n, k, s, p0, q0):
    r = (n + 4 * p0) / (n + k + 4)
    z = (s + 4 * q0) / (n + k + s + 4)
    return (r / p0) * ((1 - z) / (1 - q0))


class ColdStartTest(unittest.TestCase):
    def test_needs_fifteen_exact_verdicts_in_window(self):
        data = rows(14, 'new') + rows(30, 'known', coarse=True, weight=0.5) + rows(5, 'new', days=91)
        self.assertIsNone(build(data, NOW))
        self.assertIsNotNone(build(data + [row('skip', days=89)], NOW))

    def test_explain_cold_start(self):
        self.assertIn('冷启动', explain(None))

    def test_personal_filter(self):
        data = rows(15, 'new', personal=True) + rows(15, 'known', personal=False)
        self.assertEqual(build(data, NOW, personal=True).totals, (15.0, 0.0, 0.0))
        self.assertEqual(build(data, NOW, personal=False).totals, (0.0, 15.0, 0.0))
        self.assertEqual(build(data, NOW).totals, (15.0, 15.0, 0.0))


class FormulaTest(unittest.TestCase):
    def test_decay_half_life_and_window(self):
        data = rows(15, 'new') + [row('known', days=30), row('known', days=60), row('skip', days=90),
                                  row('skip', days=90.01), row('new', days=-1)]
        p = build(data, NOW)
        n, k, s = p.totals
        self.assertAlmostEqual(n, 16.0)          # future timestamps count as age 0
        self.assertAlmostEqual(k, 0.5 + 0.25)
        self.assertAlmostEqual(s, 0.125)         # day 90 kept at 1/8, beyond 90 dropped
        self.assertEqual(p.rated, 19)

    def test_worked_example_baseline_smoothing_weight(self):
        # Source A: 4 new, 1 skip. Source B: 6 new, 6 known, 3 skip. Totals 10/6/4, all exact, age 0.
        data = (rows(4, 'new', source='A') + rows(1, 'skip', source='A')
                + rows(6, 'new', source='B') + rows(6, 'known', source='B') + rows(3, 'skip', source='B'))
        p = build(data, NOW)
        p0, q0 = Fraction(11, 18), Fraction(5, 22)
        self.assertAlmostEqual(p.p0, float(p0))
        self.assertAlmostEqual(p.q0, float(q0))
        a = p.feature('source', 'A')
        self.assertAlmostEqual(a.rate_new, float(Fraction(29, 36)))     # (4 + 4·11/18) / (4 + 0 + 4)
        self.assertAlmostEqual(a.rate_skip, float(Fraction(7, 33)))     # (1 + 4·5/22) / (5 + 4)
        self.assertAlmostEqual(a.weight, float(Fraction(754, 561)))     # (29/22)·(52/51) ≈ 1.344
        self.assertAlmostEqual(p.weight_for_source('A'), 754 / 561)
        b = p.feature('source', 'B')
        self.assertAlmostEqual(b.weight, expected_weight(6, 6, 3, 11 / 18, 5 / 22))
        self.assertLess(b.weight, 1.0)

    def test_clamp_upper(self):
        data = rows(20, 'new', source='A') + rows(100, 'known', source='B')
        p = build(data, NOW)
        self.assertGreater(expected_weight(20, 0, 0, p.p0, p.q0), 4.0)
        self.assertEqual(p.weight_for_source('A'), 4.0)

    def test_clamp_lower(self):
        data = rows(20, 'skip', source='A') + rows(20, 'new', source='B') + rows(20, 'known', source='B')
        p = build(data, NOW)
        self.assertLess(expected_weight(0, 0, 20, p.p0, p.q0), 0.25)
        self.assertEqual(p.weight_for_source('A'), 0.25)

    def test_feature_below_five_effective_keeps_weight_one(self):
        data = (rows(20, 'known', source='B') + rows(4, 'new', source='C')
                + rows(6, 'new', source='D', days=60))          # 6 × 0.25 = 1.5 effective
        p = build(data, NOW)
        self.assertEqual(p.weight_for_source('C'), 1.0)
        self.assertAlmostEqual(p.feature('source', 'D').effective, 1.5)
        self.assertEqual(p.weight_for_source('D'), 1.0)
        self.assertEqual(p.weight_for_source('unknown'), 1.0)
        data.append(row('new', source='C'))
        self.assertGreater(build(data, NOW).weight_for_source('C'), 1.0)

    def test_coarse_weight_counts_fractionally_not_as_exact(self):
        data = rows(15, 'known', source='B') + rows(3, 'new', source='C', coarse=True, weight=1 / 3)
        p = build(data, NOW)
        self.assertEqual(p.exact, 15)
        self.assertEqual(p.rated, 18)
        self.assertAlmostEqual(p.feature('source', 'C').new, 1.0)
        self.assertAlmostEqual(p.totals[0], 1.0)

    def test_board_and_tag_dimensions(self):
        data = (rows(10, 'new', board='ai_tech', tags=['Nvidia', '芯片']) + rows(10, 'known', board='ottawa', tags=['轻轨']))
        p = build(data, NOW)
        self.assertGreater(p.weight_for_board('ai_tech'), 1.0)
        self.assertLess(p.weight_for_board('ottawa'), 1.0)
        self.assertGreater(p.weight_for_tag('Nvidia'), 1.0)
        self.assertGreater(p.weight_for_tag(' Nvidia '), 1.0)   # normalized lookup
        self.assertEqual(p.weight_for_tag('nothing'), 1.0)
        self.assertEqual(p.weight_for_board('sudbury'), 1.0)
        with self.assertRaises(ValueError):
            p.feature('publisher', 'x')

    def test_duplicate_tags_in_one_item_count_once(self):
        p = build(rows(15, 'new', tags=['AI', 'AI', ' AI']), NOW)
        self.assertEqual(p.feature('tag', 'AI').new, 15.0)

    def test_profile_is_immutable(self):
        p = build(rows(15, 'new'), NOW)
        with self.assertRaises(Exception):
            p.p0 = 0.1
        with self.assertRaises(TypeError):
            p.features['source']['X'] = None


class PromptSetsTest(unittest.TestCase):
    def base(self):
        return rows(10, 'new', source='Base') + rows(10, 'known', source='Base')

    def test_tag_set_thresholds(self):
        data = self.base()
        data += rows(4, 'known', tags=['已知A'])                 # k ≥ 2, r = 4·p0/8 ≤ 0.3 → known
        data += rows(1, 'known', tags=['太少'])                  # k < 2
        data += rows(2, 'known', tags=['混合']) + rows(1, 'new', tags=['混合'])   # r = 3/7 > 0.3
        data += rows(3, 'new', tags=['新领域'])                  # n ≥ 2, r = 5/7 ≥ 0.6 → fresh
        data += rows(5, 'skip', tags=['不想看'])                 # s ≥ 2, z = (5 + 4·8/44)/9 ≥ 0.6
        data += rows(2, 'skip', tags=['偶尔跳过']) + rows(4, 'new', tags=['偶尔跳过'])  # z < 0.6
        p = build(data, NOW)
        self.assertEqual(p.known_topics, ('已知A',))
        self.assertEqual(p.fresh_topics, ('偶尔跳过', '新领域'))   # by n_t desc
        self.assertEqual(p.not_interested, ('不想看',))
        self.assertAlmostEqual(p.p0, 19 / 37)                    # (18 + 1) / (18 + 17 + 2)
        self.assertAlmostEqual(p.feature('tag', '已知A').rate_new, 4 * 19 / 37 / 8)
        self.assertGreater(p.feature('tag', '混合').rate_new, 0.3)

    def test_sorting_limits_and_stability(self):
        data = self.base()
        for i in range(20):
            data += rows(2 + i % 4, 'known', tags=[f'K{i:02}'])
            data += rows(2 + i % 3, 'new', tags=[f'N{i:02}'])
            data += rows(2 + i % 5, 'skip', tags=[f'S{i:02}'])
        p = build(data, NOW)
        self.assertEqual(len(p.known_topics), 15)
        self.assertEqual(len(p.fresh_topics), 10)
        self.assertEqual(len(p.not_interested), 10)
        known = [p.feature('tag', t).known for t in p.known_topics]
        self.assertEqual(known, sorted(known, reverse=True))
        self.assertEqual(p.known_topics[:5], ('K03', 'K07', 'K11', 'K15', 'K19'))   # ties by name
        self.assertEqual(p.fresh_topics[:3], ('N02', 'N05', 'N08'))
        shuffled = list(data)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(build(shuffled, NOW).prompt_fragment(), p.prompt_fragment())

    def test_source_novelty_levels(self):
        data = (rows(6, 'new', source='High') + rows(6, 'known', source='Low') + rows(3, 'new', source='Mid')
                + rows(3, 'known', source='Mid') + rows(4, 'new', source='Cold'))
        p = build(data, NOW)
        levels = {entry['publisher']: entry['level'] for entry in p.source_novelty}
        self.assertEqual(levels, {'High': '高', 'Low': '低', 'Mid': '中'})
        self.assertEqual([e['publisher'] for e in p.source_novelty], ['High', 'Low', 'Mid'])

    def test_vocabulary_counts_raw_occurrences(self):
        data = self.base() + rows(3, 'new', tags=['常见']) + rows(1, 'new', tags=['罕见']) + rows(2, 'skip', tags=['两次'])
        self.assertEqual(build(data, NOW).vocabulary, ('常见', '两次'))


class FragmentTest(unittest.TestCase):
    def test_shape_and_no_urls(self):
        data = (rows(10, 'new', source='Base') + rows(10, 'known', source='Base')
                + rows(3, 'known', tags=['https://evil.example/x', '已知', '[x](y)']))
        p = build(data, NOW)
        text = p.prompt_fragment()
        data = json.loads(text)
        self.assertEqual(set(data), {'reader_profile', 'tag_vocabulary'})
        self.assertEqual(set(data['reader_profile']), {'known_topics', 'fresh_topics', 'not_interested', 'source_novelty'})
        self.assertEqual(data['reader_profile']['known_topics'], ['已知'])
        self.assertNotIn('://', text)
        self.assertNotIn('](', text)
        self.assertIn(text, p.explain())

    def heavy(self):
        data = rows(10, 'new', source='Base') + rows(10, 'known', source='Base')
        for i in range(60):
            long = f'{i:02}' + '长' * 30          # cleaned to 24 characters
            data += rows(5, 'known', tags=[f'K{long}'], source=f'Source number {i:02} ' + 'x' * 60)
            data += rows(3, 'new', tags=[f'N{long}'])
            data += rows(5, 'skip', tags=[f'S{long}'])
        return build(data, NOW)

    def test_length_cap_and_truncation_order(self):
        p = self.heavy()
        full = p.prompt_data(limit=10**6)
        self.assertEqual(len(full['tag_vocabulary']), 40)
        self.assertEqual(len(full['reader_profile']['source_novelty']), 20)
        self.assertTrue(all(len(t) <= 24 for t in full['tag_vocabulary']))
        text = p.prompt_fragment()
        self.assertLessEqual(len(text), 2500)
        got = json.loads(text)
        # Vocabulary goes first; the profile lists only once the vocabulary is empty.
        if got['tag_vocabulary']:
            self.assertEqual(got['reader_profile'], full['reader_profile'])
        self.assertEqual(got['tag_vocabulary'], full['tag_vocabulary'][:len(got['tag_vocabulary'])])
        # Any limit: a list is cut only after every list before it in TRUNCATION_ORDER is empty,
        # and each list keeps a prefix (its highest-ranked entries).
        flat = lambda d: {**d['reader_profile'], 'tag_vocabulary': d['tag_vocabulary']}
        whole = flat(full)
        self.assertTrue(all(whole[key] for key in profile.TRUNCATION_ORDER))
        for limit in range(50, 6000, 25):
            data = p.prompt_data(limit=limit)
            self.assertLessEqual(len(json.dumps(data, ensure_ascii=False, separators=(',', ':'))), max(limit, 150))
            got = flat(data)
            for i, key in enumerate(profile.TRUNCATION_ORDER):
                self.assertEqual(got[key], whole[key][:len(got[key])])
                if len(got[key]) < len(whole[key]):
                    for earlier in profile.TRUNCATION_ORDER[:i]:
                        self.assertEqual(got[earlier], [], (limit, key, earlier))
        empty = p.prompt_data(limit=10)
        self.assertTrue(all(not v for v in flat(empty).values()))

    def test_explain_reports_basis(self):
        p = build(rows(15, 'new') + rows(5, 'known', coarse=True, weight=0.5), NOW)
        text = explain(p)
        self.assertIn('精确 15 条', text)
        self.assertIn('当前发给模型的画像片段', text)


if __name__ == '__main__':
    unittest.main()
