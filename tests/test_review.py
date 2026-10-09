import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.feedback.store import FeedbackStore, snapshot
from core.inbox import DONE, DROPPED, InboxStore
from core.knowledge import review
from core.knowledge.review import (CUT_MIN_EXPOSURES, EMPTY, MAX_CHARS, Week, build_review, parse_week,
                                   previous_week, week_of_date)
from core.knowledge.store import KnowledgeDoc, KnowledgeStore

TZ = ZoneInfo('America/Toronto')
WEEK = parse_week('2026-W41', TZ)          # Mon 2026-10-05 .. Mon 2026-10-12
MID = WEEK.start_ts + 2 * 86400            # Wednesday
NOW = WEEK.end_ts + 9 * 3600               # the Monday morning after


class WeekTests(unittest.TestCase):
    def test_week_starts_monday_midnight_local(self):
        self.assertEqual(WEEK.key, '2026-W41')
        self.assertEqual(WEEK.start, datetime(2026, 10, 5, tzinfo=TZ))
        self.assertEqual(WEEK.end, datetime(2026, 10, 12, tzinfo=TZ))
        self.assertEqual(WEEK.label, '10/05–10/11')
        self.assertEqual(WEEK.end_ts - WEEK.start_ts, 7 * 86400)

    def test_weeks_across_dst_changes(self):
        fall = parse_week('2026-W44', TZ)    # DST ends Sunday 2026-11-01
        self.assertEqual(fall.end_ts - fall.start_ts, 169 * 3600)
        spring = parse_week('2026-W10', TZ)  # DST starts Sunday 2026-03-08
        self.assertEqual(spring.end_ts - spring.start_ts, 167 * 3600)
        # Consecutive weeks share their boundary exactly.
        self.assertEqual(parse_week('2026-W45', TZ).start_ts, fall.end_ts)
        self.assertEqual(parse_week('2026-W45', TZ).previous(), fall)

    def test_previous_week_and_parsing(self):
        self.assertEqual(previous_week(datetime(2026, 10, 12, 9, 0, tzinfo=TZ), TZ), WEEK)
        self.assertEqual(previous_week(datetime(2026, 10, 11, 23, 59, tzinfo=TZ), TZ).key, '2026-W40')
        # A UTC moment is judged in the bot timezone: Monday 02:00 UTC is still Sunday in Toronto.
        self.assertEqual(previous_week(datetime(2026, 10, 12, 2, 0, tzinfo=ZoneInfo('UTC')), TZ).key, '2026-W40')
        self.assertEqual(parse_week('2026w41', TZ), WEEK)
        self.assertEqual(week_of_date(datetime(2027, 1, 1).date(), TZ).key, '2026-W53')
        for bad in ('', '2026-41', '2026-W54', '2025-W53', 'last'):
            with self.assertRaises(ValueError, msg=bad):
                parse_week(bad, TZ)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.feedback = FeedbackStore(root / 'feedback.sqlite3', now=0)
        self.addCleanup(self.feedback.close)
        self.knowledge = KnowledgeStore(root / 'knowledge.sqlite3')
        self.addCleanup(self.knowledge.close)
        self.inbox = InboxStore(root / 'inbox')
        self.n = 0

    def expose(self, source, *, at=MID, personal=True, category='Ottawa', verdict=None, coarse_n=None):
        self.n += 1
        url = f'https://example.com/{self.n}'
        snap = snapshot({'url': url, 'title': f'中文{self.n}',
                         '_evidence': {'url': url, 'title': f'Story {self.n}', 'publisher': source,
                                       'category': category}})
        with self.feedback._lock, self.feedback.db:
            self.feedback._expose(snap, ref_kind='run', ref_id=self.n, subscription='following',
                                  personal=personal, channel_id=1, message_id=None, position=0,
                                  item_count=1, shown_at=at)
        if verdict:
            if coarse_n:
                self.feedback.record(snap['key'], verdict, via='reaction', weight=1 / coarse_n, coarse=True,
                                     now=at + 60)
            else:
                self.feedback.record(snap['key'], verdict, via='button', now=at + 60)
        return snap['key']

    def build(self, **kwargs):
        kwargs.setdefault('feedback', self.feedback)
        return build_review(WEEK, now=NOW, **kwargs)


class FeedbackSectionTests(Fixture):
    def test_exposures_and_exact_and_coarse_verdicts(self):
        self.expose('CBC Ottawa', verdict='new')
        self.expose('CBC Ottawa', verdict='new')
        self.expose('CBC Ottawa', verdict='known')
        self.expose('CBC Ottawa', verdict='skip')
        self.expose('BBC World', personal=False, category='World', verdict='new', coarse_n=4)
        self.expose('BBC World', personal=False, category='World', verdict='known', coarse_n=2)
        self.expose('BBC World', personal=False, category='World')
        # Outside the week: the Sunday before and the following Monday 00:00 (exclusive end).
        self.expose('CBC Ottawa', at=WEEK.start_ts - 3600, verdict='new')
        self.expose('CBC Ottawa', at=WEEK.end_ts, verdict='new')
        # Last second of Sunday local time is inside.
        self.expose('CBC Ottawa', at=WEEK.end_ts - 1)

        result = self.build()
        self.assertEqual(result.exposures, {'personal': 5, 'shared': 3, 'total': 8})
        v = result.verdicts
        self.assertEqual(v['exact'], {'new': 2, 'known': 1, 'skip': 1})
        self.assertAlmostEqual(v['coarse']['new'], 0.25)
        self.assertAlmostEqual(v['coarse']['known'], 0.5)
        self.assertAlmostEqual(v['total']['new'], 2.25)
        self.assertEqual(v['rated'], 6)
        self.assertAlmostEqual(v['new_rate'], 2.25 / 3.75)
        text = result.render()
        self.assertIn('个人 5 条 · 共享 3 条', text)
        self.assertIn('精确：🆕 2 · 👌 1 · 🚫 1', text)
        self.assertIn('粗（反应按 1/n 加权）：🆕 0.2 · 👌 0.5 · 🚫 0', text)
        self.assertIn('新知率 60%', text)

    def test_top_sources_zero_new_and_cut_candidates(self):
        for index, name in enumerate(['S1', 'S2', 'S3', 'S4', 'S5', 'S6']):
            for _ in range(6 - index):
                self.expose(name, verdict='new')
        for _ in range(CUT_MIN_EXPOSURES):
            self.expose('Dull', verdict='known')
        for _ in range(CUT_MIN_EXPOSURES - 1):
            self.expose('Quiet')
        for _ in range(CUT_MIN_EXPOSURES):
            self.expose('Mixed', verdict='known')
        self.expose('Mixed', verdict='new')
        # A verdict this week on an item shown last week: rated, but not "exposed this week".
        old = self.expose('Old', at=WEEK.start_ts - 86400)
        self.feedback.record(old, 'known', via='button', now=MID)

        result = self.build(catalog=[{'name': 'Dull', 'personal': True}, {'name': 'Never', 'personal': True},
                                     {'name': 'Quiet', 'personal': False}])
        self.assertEqual([s.name for s in result.top_sources], ['S1', 'S2', 'S3', 'S4', 'S5'])
        self.assertEqual(result.top_sources[0].new, 6)
        self.assertEqual([s.name for s in result.zero_new_sources], ['Dull', 'Quiet'])
        self.assertEqual([s.name for s in result.cut_candidates], ['Dull'])
        self.assertTrue(result.cut_candidates[0].personal)
        self.assertEqual(result.unexposed_personal, ('Never',))
        text = result.render()
        self.assertIn('1. `S1` 🆕 6 · 曝光 6 · 新知率 100%', text)
        self.assertNotIn('`S6`', text)
        self.assertIn('零新知（本周有曝光）：`Dull`(3)、`Quiet`(2)', text)
        self.assertIn('- `Dull` 曝光 3 · 已评 3', text)
        self.assertIn('本周没被推送的个人源：`Never`', text)

    def test_board_line_and_trend(self):
        self.expose('CBC Ottawa', verdict='new')
        self.expose('Globe Investing', category='Investing', verdict='known')
        self.expose('CBC Ottawa', at=WEEK.start_ts - 3 * 86400, verdict='known')
        result = self.build()
        self.assertEqual([b['board'] for b in result.boards], ['ottawa', 'invest'])
        self.assertEqual([key for key, _ in result.trend], ['2026-W38', '2026-W39', '2026-W40', '2026-W41'])
        self.assertEqual([rate for _, rate in result.trend], [None, None, 0.0, 0.5])
        text = result.render()
        self.assertIn('板块：Ottawa 1/🆕1/👌0/🚫0 · 投资 1/🆕0/👌1/🚫0', text)
        self.assertIn('近 4 周新知率：W38 — → W39 — → W40 0% → W41 50%', text)

    def test_profile_cold_start_counts_missing_exact_verdicts(self):
        for _ in range(4):
            self.expose('CBC Ottawa', verdict='new')
        self.expose('BBC World', verdict='known', coarse_n=3)
        lines = self.build().profile_lines
        self.assertEqual(len(lines), 1)
        self.assertIn('精确反馈 4 条，还差 11 条', lines[0])

    def test_profile_summary_after_cold_start(self):
        for _ in range(16):
            self.expose('CBC Ottawa', verdict='new')
        lines = self.build().profile_lines
        self.assertTrue(lines[0].startswith('读者画像：近 90 天 16 条反馈'))
        self.assertTrue(all('reader_profile' not in line for line in lines))


class OtherSectionTests(Fixture):
    def at(self, ts):
        return datetime.fromtimestamp(ts, TZ)

    def test_inbox_counts_and_backlog(self):
        a, _ = self.inbox.save(url='https://a.example/1', title='读完的', body='', now=self.at(MID))
        b, _ = self.inbox.save(url='https://a.example/2', title='丢掉的', body='', now=self.at(MID))
        self.inbox.save(url='https://a.example/3', title='还没读', body='', now=self.at(MID))
        old, _ = self.inbox.save(url='https://a.example/4', title='很早的', body='',
                                 now=self.at(WEEK.start_ts - 20 * 86400))
        self.inbox.set_state(a['id'], DONE, now=self.at(MID + 3600))
        self.inbox.set_state(b['id'], DROPPED, now=self.at(MID + 3600))
        done_last_week, _ = self.inbox.save(url='https://a.example/5', title='上周读完', body='',
                                            now=self.at(WEEK.start_ts - 86400))
        self.inbox.set_state(done_last_week['id'], DONE, now=self.at(WEEK.start_ts - 3600))

        result = self.build(feedback=None, inbox=self.inbox)
        self.assertEqual({k: result.inbox[k] for k in ('saved', 'done', 'dropped', 'backlog')},
                         {'saved': 3, 'done': 1, 'dropped': 1, 'backlog': 2})
        self.assertEqual(result.inbox['oldest_days'], int((NOW - (WEEK.start_ts - 20 * 86400)) // 86400))
        text = result.render()
        self.assertIn('存入 3 · 读完 1 · 丢弃 1 · 积压 2（最老 27 天）', text)
        self.assertIn('- 还没读', text)

    def test_recall_uses_knowledge_stats_and_silent_sources(self):
        self.knowledge.bump_usage('recall', '2026-10-04')   # Sunday before
        self.knowledge.bump_usage('recall', '2026-10-05')
        self.knowledge.bump_usage('recall', '2026-10-11')
        self.knowledge.bump_usage('recall', '2026-10-11')
        self.knowledge.bump_usage('recall', '2026-10-12')   # next Monday
        self.knowledge.upsert_docs([KnowledgeDoc('news:1', 'news', 'T', 'B', MID, source='Alive')])
        result = self.build(feedback=None, knowledge=self.knowledge,
                            catalog=[{'name': 'Alive', 'personal': True}, {'name': 'Dead', 'personal': False}])
        self.assertEqual(result.recall_uses, 3)
        self.assertEqual(result.silent_sources, ('Dead',))
        self.assertEqual(result.knowledge['docs'], 1)
        text = result.render()
        self.assertIn('本周使用 3 次', text)
        self.assertIn('本周没有新素材的信源（疑似失效）：`Dead`', text)
        self.assertIn('-# 知识库 1 篇', text)

    def test_silent_sources_need_a_running_sync(self):
        result = self.build(feedback=None, knowledge=self.knowledge, catalog=[{'name': 'Dead', 'personal': True}])
        self.assertEqual(result.silent_sources, ())

    def test_everything_empty_shows_placeholder(self):
        result = self.build(feedback=None)
        text = result.render()
        self.assertEqual(text.count(EMPTY), 7)
        self.assertIn('**⑤ 追踪**\n暂无', text)
        # Present but empty stores render the same placeholders.
        text = self.build(inbox=self.inbox, knowledge=self.knowledge).render()
        self.assertIn('**④ 收件箱**\n暂无', text)
        self.assertIn('**② 新知 / 已知 / 不关心**\n暂无', text)
        self.assertIn('本周使用 0 次', text)

    def test_watch_summary_when_available(self):
        class Watch:
            def weekly_summary(self, start, end):
                assert (start, end) == (WEEK.start_ts, WEEK.end_ts)
                return [{'name': '储能', 'hits': 3, 'delivered': 2, 'rejected': 1, 'uncertain': 0}]

        text = self.build(feedback=None, watch=Watch()).render()
        self.assertIn('- `储能`：命中 3 · 已推送 2 · 否决 1 · 不确定 0', text)

    def test_a_failing_store_only_empties_its_section(self):
        class Broken:
            def items(self):
                raise OSError('disk')

        self.expose('CBC Ottawa', verdict='new')
        result = self.build(inbox=Broken())
        self.assertEqual(result.errors, ('收件箱',))
        self.assertEqual(result.exposures['personal'], 1)
        self.assertIn('部分统计读取失败：收件箱', result.render())


class RenderTests(unittest.TestCase):
    def test_render_never_exceeds_the_embed_limit_and_escapes_markdown(self):
        lines = tuple(review.SourceLine(f'**Source** [{i}](x) ' + 'n' * 80, 9, 9, 0, 9, 0, 0.0, True)
                      for i in range(200))
        result = review.WeeklyReview(
            week=WEEK, exposures={'personal': 9, 'shared': 9, 'total': 18},
            verdicts={'exact': {'new': 1, 'known': 2, 'skip': 3}, 'coarse': {'new': 0, 'known': 0, 'skip': 0},
                      'total': {'new': 1, 'known': 2, 'skip': 3}, 'rated': 6, 'new_rate': 1 / 3},
            top_sources=lines[:5], zero_new_sources=lines, cut_candidates=lines,
            unexposed_personal=tuple(f'P{i}' * 20 for i in range(100)),
            silent_sources=tuple(f'D{i}' * 20 for i in range(100)),
            inbox={'saved': 1, 'done': 0, 'dropped': 0, 'backlog': 1, 'oldest_days': 1,
                   'titles': ('`x`' * 100,) * 10},
            profile_lines=('x' * 3000,))
        text = result.render()
        self.assertLessEqual(len(text), MAX_CHARS)
        self.assertTrue(text.endswith('…（超出长度，已截断）'))
        self.assertNotIn('**Source**', text)
        self.assertNotIn('](x)', text)
        self.assertIn('等 200 个', text)
        self.assertLessEqual(len(result.render(max_chars=500)), 500)
        self.assertIn('2026-W41', result.title)
        self.assertEqual(result.to_dict()['week']['key'], '2026-W41')


if __name__ == '__main__':
    unittest.main()
