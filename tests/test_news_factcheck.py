"""Deterministic digest fact-check: unit cases, labelled-error regressions, pipeline wiring."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from core.ai_providers import AIResult
from core.news import factcheck
from core.news.topics import TOPICS
from core.news.topics.general import MAX_RENDERED_SUMMARY_CHARS, MAX_RENDERED_TITLE_CHARS
from tests.test_news_pipeline import PipelineFixture, article, subscription

PUBLISHED = '2026-10-12T14:00:00+00:00'

# Regression cases rebuilt from the error classes labelled in the 2026-10-19 accuracy review
# (wrong person, changed number, added fact). Each: source title, source summary, Chinese
# title, Chinese summary. The checker must flag every one.
LABELLED_ERRORS = [
    ('Gantz says he will quit Israel war cabinet unless Gaza plan adopted by June 8',
     'Benny Gantz, a centrist member of the war cabinet, gave Prime Minister Benjamin Netanyahu an ultimatum.',
     '加兰特威胁退出以色列战时内阁', '战时内阁成员加兰特要求内塔尼亚胡在6月8日前通过加沙战后计划。'),
    ('Gantz says he will quit Israel war cabinet unless Gaza plan adopted by June 8',
     'Benny Gantz, a centrist member of the war cabinet, gave Prime Minister Benjamin Netanyahu an ultimatum.',
     '以色列战时内阁成员威胁退出', '加兰特（Yoav Gallant）要求内塔尼亚胡在6月8日前通过加沙计划。'),
    ('Arteta says Arsenal must learn from Champions League exit',
     'Mikel Arteta said his side showed character but lacked composure in the second leg.',
     '温格：阿森纳须从欧冠出局中吸取教训', '温格表示球队展现了斗志，但次回合缺乏冷静。'),
    ('Pope Leo XIV calls for ceasefire in first Sunday address',
     'Pope Leo XIV used his Angelus address to appeal for an end to fighting in Ukraine and Gaza.',
     '教皇方济各呼吁停火', '教皇方济各在三钟经祈祷中呼吁乌克兰和加沙停止战斗。'),
    ('Canada to buy 88 F-35 fighter jets in phased deal',
     'Ottawa confirmed the purchase of 88 jets, with the first 16 arriving in 2026.',
     '加拿大将分阶段采购98架F-35战机', '渥太华确认采购，首批16架将于2026年交付。'),
    ('Bank of Canada cuts key rate to 2.5%',
     'The central bank lowered its policy rate by a quarter point, citing slowing growth.',
     '加拿大央行降息至2.25%', '央行将政策利率下调25个基点，理由是增长放缓。'),
    ('Ontario wildfire forces evacuation of 1,200 residents',
     'About 1,200 people were ordered to leave their homes near Sudbury as the fire grew.',
     '安省山火迫使1.2万居民撤离', '萨德伯里附近约1.2万人被要求离家。'),
    ('Tesla recalls 2 million vehicles over Autopilot',
     'The recall covers nearly all vehicles sold in the US since the feature launched.',
     '特斯拉召回200万辆车', '召回涉及美国自2015年以来售出的几乎所有车辆，马斯克尚未回应。'),
    ('Ottawa council approves $45 million transit plan',
     'Councillors voted 18-6 in favour of the bus network redesign.',
     '渥太华市议会批准4500万元交通计划', '议员以19比6投票通过公交线路重组方案。'),
    ('Nvidia shares fall as export curbs bite',
     'Shares dropped 6% after the company warned of a $5.5 billion charge.',
     '英伟达股价下跌', '公司警告将计提55亿美元费用后，股价下跌8%。'),
    ('Zelensky meets European leaders in London',
     'Volodymyr Zelensky held talks with Keir Starmer and Emmanuel Macron on security guarantees.',
     '泽连斯基在伦敦会见欧洲领导人', '泽连斯基与斯塔默、马克龙及默茨讨论安全保障。'),
    ('Carney says Canada will meet NATO spending target this year',
     'Prime Minister Mark Carney said defence spending will reach 2% of GDP.',
     '卡尼称加拿大今年达到北约军费目标', '总理卡尼（Justin Trudeau）表示国防开支将达到GDP的2%。'),
]

# Correct translations, including the formats a careful model produces; none may be flagged.
CLEAN = [
    ('Gantz says he will quit Israel war cabinet unless Gaza plan adopted by June 8',
     "Benny Gantz, a centrist member of the war cabinet, gave Netanyahu's government an ultimatum.",
     '甘茨威胁退出以色列战时内阁', '战时内阁成员甘茨（Benny Gantz）要求内塔尼亚胡政府在6月8日前通过加沙计划。'),
    ('Arteta says Arsenal must learn from Champions League exit',
     'Mikel Arteta said his side showed character but lacked composure in the second leg.',
     '阿尔特塔：阿森纳须吸取教训', '阿尔特塔（Mikel Arteta）表示球队展现斗志，但次回合缺乏冷静。'),
    ('Pope Leo XIV calls for ceasefire in first Sunday address',
     'Pope Leo XIV used his Angelus address to appeal for an end to fighting in Ukraine and Gaza.',
     '教皇利奥十四世呼吁停火', '教皇利奥十四世（Pope Leo XIV）在三钟经祈祷中呼吁乌克兰和加沙停火。'),
    ('Canada to buy 88 F-35 fighter jets in phased deal',
     'Ottawa confirmed the purchase of 88 jets, with the first 16 arriving in 2026.',
     '加拿大将采购88架F-35战机', '渥太华确认采购，首批16架将于2026年交付。'),
    ('Bank of Canada cuts key rate to 2.5%',
     'The central bank lowered its policy rate by a quarter point to 2.5 per cent.',
     '加拿大央行降息至2.5%', '央行下调政策利率至2.5%。'),
    ('Ontario wildfire forces evacuation of 1,200 residents',
     'About 1,200 people were ordered to leave their homes near Sudbury.',
     '安省山火迫使1200居民撤离', '萨德伯里附近约1,200人被要求离家。'),
    ('Tesla recalls 2 million vehicles over Autopilot',
     'The recall covers 2,031,220 vehicles sold in the US.',
     '特斯拉召回200万辆车', '召回涉及在美国售出的约203万辆车。'),
    ('Microsoft to invest $80bn in AI data centres',
     'Microsoft plans to spend $80 billion this fiscal year, more than half in the United States.',
     '微软将投资800亿美元建AI数据中心', '微软计划本财年投入800亿美元，超过一半在美国。'),
    ('Unemployment rises to 7.1 per cent in September',
     'Statistics Canada said the economy shed 14,000 jobs last month.',
     '9月失业率升至7.1%', '加拿大统计局称上月减少1.4万个就业岗位。'),
    ('Ottawa council approves $45 million transit plan',
     'Councillors voted 18-6 in favour of the bus network redesign on Wednesday.',
     '渥太华议会批准4500万元交通计划', '议员周三以18比6通过公交线路重组。'),
    ('Two killed, three injured in Highway 17 crash near Sudbury',
     'OPP said a transport truck and a pickup collided early Sunday.',
     '萨德伯里附近17号公路车祸致2死3伤', '安省警察称一辆货车与皮卡周日清晨相撞。'),
    ('OpenAI releases GPT-5 to all ChatGPT users',
     "OpenAI's new model is available to free and paid users starting today.",
     'OpenAI向所有ChatGPT用户开放GPT-5', 'OpenAI的新模型今天起向免费和付费用户开放。'),
    ('Zelenskyy meets European leaders in London',
     'Volodymyr Zelensky held talks with Keir Starmer on security guarantees.',
     '泽连斯基在伦敦会见欧洲领导人', '泽连斯基（Volodymyr Zelenskyy）与斯塔默（Keir Starmer）讨论安全保障。'),
    ('Jean-Luc Mélenchon calls for snap vote',
     'The far-left leader said parliament should be dissolved.',
     '梅朗雄呼吁提前选举', '极左领导人梅朗雄（Jean Luc Melenchon）称应解散议会。'),
    ('Fed holds rates steady, signals two cuts later this year',
     'Chair Jerome Powell said the committee needs more confidence on inflation.',
     '美联储按兵不动，暗示年内降息两次', '主席鲍威尔（Powell）表示需要对通胀更有信心。'),
    ('Storm leaves 30,000 without power in Ottawa',
     'Hydro Ottawa said crews were working through the night of Oct. 11.',
     '风暴致渥太华3万户停电', 'Hydro Ottawa称抢修人员10月11日连夜作业。'),
]


def as_item(source_title, source_summary, title, summary, *, general=True):
    evidence = {'title': source_title, 'rss_summary': source_summary, 'content': source_summary,
                'publisher': 'Example News', 'category': 'World', 'published_at': PUBLISHED, 'url': 'https://e.com/x'}
    if general:
        return {'title': source_title, 'rss_summary': source_summary, 'publisher': 'Example News',
                'category': 'World', 'url': 'https://e.com/x', '_evidence': evidence,
                'digest_title': title, 'digest_summary': summary}
    return {'title': title, 'summary': summary, 'why_read': '可以想一想：这会改变什么？', 'content': source_summary,
            'publisher': 'Example News', 'url': 'https://e.com/x', '_evidence': evidence}


class NumberTests(unittest.TestCase):
    def check(self, output, source, published_at=PUBLISHED):
        return factcheck.unsupported_numbers(output, source, published_at=published_at)

    def test_formats_and_scales_match(self):
        self.assertEqual(self.check('1000人、1,000人、1千人', 'About 1,000 people'), [])
        self.assertEqual(self.check('15亿美元', '$1.5 billion'), [])
        self.assertEqual(self.check('1.5万亿美元', '$1.5 trillion'), [])
        self.assertEqual(self.check('500万加元', 'C$5m grant'), [])
        self.assertEqual(self.check('上涨5%', 'rose 5 per cent'), [])
        self.assertEqual(self.check('3人死亡', 'three people died'), [])
        self.assertEqual(self.check('约1.2亿', '$118 million'), [])  # rounding at the output precision
        self.assertEqual(self.check('下调25个基点', 'cut by a quarter point'), [])
        self.assertEqual(self.check('近20年来', 'for two decades'), [])
        self.assertEqual(self.check('50岁男子', 'a 50-year-old man'), [])
        self.assertEqual(self.check('3比1获胜', 'won 3-1'), [])

    def test_changed_numbers_are_flagged(self):
        self.assertEqual(self.check('1.5亿美元', '$1.5 billion'), ['1.5亿'])
        self.assertEqual(self.check('上涨6%', 'rose 5 per cent'), ['6'])
        self.assertEqual(self.check('98架', '88 jets'), ['98'])

    def test_dates_from_published_at_and_month_names_pass(self):
        self.assertEqual(self.check('10月12日', 'on Sunday'), [])
        self.assertEqual(self.check('2026年', 'this year'), [])
        self.assertEqual(self.check('6月8日', 'by June 8'), [])
        self.assertEqual(self.check('2019年', 'years ago'), ['2019'])
        self.assertEqual(self.check('2025年销量', 'sales last year'), [])
        self.assertEqual(self.check('2024年销量', 'sales last year'), ['2024'])

    def test_kilometres_are_not_a_thousand_scale(self):
        self.assertEqual(self.check('10千米', '10 km'), [])


class NameTests(unittest.TestCase):
    def test_bracket_name_must_be_in_source(self):
        source = "Benny Gantz's party said"
        self.assertEqual(factcheck.unsupported_names('甘茨（Benny Gantz）', source), [])
        self.assertEqual(factcheck.unsupported_names('甘茨（Gantz）', source), [])
        self.assertEqual(factcheck.unsupported_names('加兰特（Yoav Gallant）', source), ['Yoav Gallant'])

    def test_titles_numerals_and_acronyms_do_not_clear_or_flag(self):
        self.assertEqual(factcheck.unsupported_names('方济各（Pope Francis）', 'Pope Leo XIV said'), ['Pope Francis'])
        self.assertEqual(factcheck.unsupported_names('北约（NATO）与人工智能（AI）', 'the alliance'), [])

    def test_case_accent_hyphen_and_one_letter_variants(self):
        self.assertEqual(factcheck.unsupported_names('（Jean Luc Melenchon）', 'Jean-Luc Mélenchon'), [])
        self.assertEqual(factcheck.unsupported_names('（Zelenskyy）', 'Volodymyr Zelensky'), [])
        self.assertEqual(factcheck.unsupported_names('nvidia与Nvidia', 'NVIDIA said'), [])

    def test_bare_latin_proper_nouns_checked_but_acronyms_and_lowercase_skipped(self):
        self.assertEqual(factcheck.unsupported_names('Wenger称', 'Arteta said'), ['Wenger'])
        self.assertEqual(factcheck.unsupported_names('AI与GDP及iPhone', 'nothing'), [])

    def test_publisher_counts_as_source(self):
        item = as_item('Storm hits', 'Crews out.', '风暴来袭', '据Reuters报道，抢修人员出动。')
        item['_evidence']['publisher'] = 'Reuters'
        self.assertEqual(factcheck.check_item(item, ('digest_title', 'digest_summary')), [])

    def test_glossary_chinese_only_names(self):
        self.assertEqual(factcheck.unsupported_glossary('温格表示', 'Mikel Arteta said'), ['温格'])
        self.assertEqual(factcheck.unsupported_glossary('阿尔特塔表示', 'Mikel Arteta said'), [])


class LabelledRegressionTests(unittest.TestCase):
    def test_every_labelled_error_is_caught_on_both_topic_shapes(self):
        for case in LABELLED_ERRORS:
            for general in (True, False):
                fields = ('digest_title', 'digest_summary') if general else ('title', 'summary', 'why_read')
                with self.subTest(case=case[2], general=general):
                    self.assertTrue(factcheck.check_item(as_item(*case, general=general), fields))

    def test_no_clean_translation_is_flagged(self):
        for case in CLEAN:
            for general in (True, False):
                fields = ('digest_title', 'digest_summary') if general else ('title', 'summary', 'why_read')
                with self.subTest(case=case[2], general=general):
                    self.assertEqual(factcheck.check_item(as_item(*case, general=general), fields), [])


class GuardTests(unittest.TestCase):
    def test_general_fallback_keeps_item_with_original_text_within_budget(self):
        good, bad = as_item(*CLEAN[0]), as_item(*LABELLED_ERRORS[0])
        bad['_evidence']['rss_summary'] = 'x' * 600
        result = TOPICS['general'].factcheck([good, bad])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['digest_title'], CLEAN[0][2])
        self.assertTrue(result[0]['_factcheck']['ok'])
        self.assertTrue(result[1]['digest_title'].startswith(factcheck.ORIGINAL_MARK + 'Gantz says'))
        self.assertFalse(result[1]['_factcheck']['ok'])
        self.assertIn('译名:加兰特', result[1]['_factcheck']['reasons'])
        self.assertLessEqual(len(result[1]['digest_title']) + len(result[1]['digest_summary']),
                             MAX_RENDERED_TITLE_CHARS + MAX_RENDERED_SUMMARY_CHARS)
        # Still renders within the Discord budget.
        TOPICS['general'].render(result, '早间', 'Test (model)')

    def test_discovery_fallback_clears_why_read_and_renders(self):
        bad = as_item(*LABELLED_ERRORS[2], general=False)
        bad.update(published_at=PUBLISHED)
        result = TOPICS['discovery'].factcheck([bad])
        self.assertTrue(result[0]['title'].startswith('「原文」Arteta says'))
        self.assertEqual(result[0]['why_read'], '')
        body = TOPICS['discovery'].render(result, '早间', 'Test')[0].description
        self.assertIn('原文', body)
        self.assertNotIn('值得一读', body)

    def test_display_length_allows_person_brackets(self):
        text = '甲' * 40 + '（Benny Gantz）'
        self.assertEqual(factcheck.display_length(text), 40)

    def test_parse_attribution(self):
        self.assertEqual(factcheck.parse_attribution('Groq (qwen/qwen3-32b)'), ('Groq', 'qwen/qwen3-32b'))
        self.assertEqual(factcheck.parse_attribution('odd'), ('odd', ''))

    def test_prompts_carry_the_person_name_rule(self):
        self.assertIn('甘茨（Benny Gantz）', TOPICS['discovery'].system)
        self.assertIn('甘茨（Benny Gantz）', TOPICS['following'].system)
        prepared = TOPICS['general'].prepare([], subscription(), [], '早间')
        self.assertIn('甘茨（Benny Gantz）', prepared.system)


class PipelineFactcheckTests(PipelineFixture):
    async def test_failed_item_is_delivered_as_original_and_run_records_model(self):
        self.store.upsert_articles([article(1, content='A concrete observation, with a comparison.')])

        async def wrong_number(raw, **kwargs):
            identity = json.loads(raw)['candidates'][0]['id']
            item = {'id': identity, 'title': '有依据的中文标题', 'summary': '原文提供了99项新的具体观察。'}
            return AIResult(json.dumps({'items': [item]}, ensure_ascii=False), 'Groq', 'qwen/qwen3-32b')

        self.model.side_effect = wrong_number
        await self.publish()
        self.channel.send.assert_awaited_once()
        description = self.channel.send.await_args.kwargs['embeds'][0].description
        self.assertIn('「原文」Original story 1', description)
        run = self.store.get_run(1)
        self.assertEqual(run['status'], 'delivered')
        [item] = json.loads(run['payload'])
        self.assertEqual((item['_provider'], item['_model']), ('Groq', 'qwen/qwen3-32b'))
        self.assertFalse(item['_factcheck']['ok'])
        self.assertIn('数字:99', item['_factcheck']['reasons'])

    async def test_passing_item_keeps_chinese_text_and_records_model(self):
        self.store.upsert_articles([article(1)])
        await self.publish()
        [item] = json.loads(self.store.get_run(1)['payload'])
        self.assertEqual(item['digest_title'], '有依据的中文标题')
        self.assertTrue(item['_factcheck']['ok'])
        self.assertEqual((item['_provider'], item['_model']), ('Test', 'model'))


if __name__ == '__main__':
    unittest.main()
