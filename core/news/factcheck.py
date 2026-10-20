"""Deterministic, free fact-check of model digest text against the item's own source.

Runs after selection, per item, with no model call. It only flags what it can prove is
missing from the source the model saw (the candidate title plus RSS summary/content):

1. numbers: every Arabic number in the Chinese title/summary (with 千/万/亿 scales and
   percentages) must match a source number, allowing rounding at the output's precision;
2. Latin names: every Latin-script name the output carries (the person-name brackets
   required by the prompt, and proper-noun-like words) must occur in the source;
3. glossary: a few frequently confused people, written in Chinese only, must have their
   Latin name in the source (catches 甘茨→加兰特, Arteta→温格, Leo XIV→方济各).

It is deliberately conservative: anything it cannot parse passes (false negatives over
false alarms). A failed item is not dropped; `fallback_fields` delivers the source's own
English title and a short original summary marked 「原文」 instead of the Chinese text.
"""
import datetime
import math
import re
import unicodedata

ORIGINAL_MARK = '「原文」'
# Shared by every digest prompt (general, discovery, following).
PERSON_NAME_RULE = (
    "人物第一次出现时写中文译名，紧跟全角括号内的原文拉丁字母全名，括号内照抄候选原文的拼写，"
    "例如 甘茨（Benny Gantz）；原文只写了姓就只写姓。括号写在摘要中该人物第一次出现处，标题只用中文名"
    "（人物只出现在标题时才在标题加括号）。拿不准中文译名时直接写原文名。机构和地名照常，不加括号。"
    "数字、年份和百分比照抄原文，不自行换算或推算。"
)

# Brackets the prompt asks for after a person's Chinese name: 甘茨（Benny Gantz）.
_BRACKET = re.compile(r'[（(]\s*([A-Za-zÀ-ɏ][A-Za-zÀ-ɏ .\'’\-]{0,60}?)\s*[)）]')
_LATIN_WORD = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’\-]*[A-Za-zÀ-ɏ]|[A-Za-zÀ-ɏ]")
# Titles, honorifics, particles and Roman numerals do not identify a person.
_NON_IDENTIFYING = {
    'pope', 'king', 'queen', 'prince', 'princess', 'president', 'premier', 'minister', 'prime',
    'mayor', 'chief', 'general', 'gen', 'dr', 'mr', 'mrs', 'ms', 'sir', 'dame', 'lord', 'lady',
    'saint', 'st', 'jr', 'sr', 'de', 'da', 'del', 'der', 'den', 'di', 'du', 'la', 'le', 'van',
    'von', 'bin', 'ibn', 'al', 'el', 'the', 'of', 'and', 'ii', 'iii', 'iv', 'v', 'vi', 'vii',
    'viii', 'ix', 'x', 'xi', 'xii', 'xiii', 'xiv', 'xv', 'xvi',
}

# Small glossary of people the free models confuse. Chinese spellings → Latin forms, any of
# which in the source clears the name. Only distinctive multi-character spellings, so a
# common word never matches. Extend from labelled errors, keep it short.
GLOSSARY = (
    (('甘茨',), ('Gantz',)),
    (('加兰特',), ('Gallant',)),
    (('阿尔特塔', '阿特塔'), ('Arteta',)),
    (('温格',), ('Wenger',)),
    (('方济各',), ('Francis', 'Francesco', 'Francisco')),
    (('利奥十四世', '良十四世', '利奥', '良十四'), ('Leo', 'Prevost')),
    (('内塔尼亚胡',), ('Netanyahu',)),
    (('泽连斯基',), ('Zelensky', 'Zelenskyy', 'Zelenskiy')),
    (('普京',), ('Putin',)),
    (('特朗普',), ('Trump',)),
    (('拜登',), ('Biden',)),
    (('卡尼',), ('Carney',)),
    (('特鲁多',), ('Trudeau',)),
    (('博励治', '普瓦利耶夫'), ('Poilievre',)),
    (('马斯克',), ('Musk',)),
    (('斯塔默',), ('Starmer',)),
    (('马克龙',), ('Macron',)),
    (('默茨',), ('Merz',)),
    (('鲍威尔',), ('Powell',)),
    (('哈梅内伊',), ('Khamenei',)),
    (('埃尔多安',), ('Erdogan', 'Erdoğan')),
    (('莫迪',), ('Modi',)),
    (('万斯',), ('Vance',)),
)

# ---- numbers -------------------------------------------------------------------------

_CN_SCALE = {'万亿': 1e12, '千亿': 1e11, '百亿': 1e10, '亿': 1e8, '千万': 1e7, '百万': 1e6, '万': 1e4, '千': 1e3}
_OUT_NUMBER = re.compile(r'(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(万亿|千亿|百亿|亿|千万|百万|万|千(?![米克瓦卡]))?')
_SRC_NUMBER = re.compile(r'(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*-?\s*(trillion|billion|million|thousand|bn|mn|tn|[kmb])\b)?',
                         re.IGNORECASE)
_EN_SCALE = {'trillion': 1e12, 'tn': 1e12, 'billion': 1e9, 'bn': 1e9, 'b': 1e9, 'million': 1e6, 'mn': 1e6, 'm': 1e6,
             'thousand': 1e3, 'k': 1e3}
_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
    'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16,
    'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
    'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100, 'dozen': 12, 'half': 0.5,
    'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6, 'seventh': 7, 'eighth': 8,
    'ninth': 9, 'tenth': 10, 'once': 1, 'twice': 2, 'single': 1, 'double': 2, 'triple': 3, 'a': 1, 'an': 1,
}
_WORD_NUMBER = re.compile(r'\b(' + '|'.join(sorted(_WORDS, key=len, reverse=True)) + r')\b'
                          r'(?:[\s-]+(hundred|thousand|million|billion|trillion|decades?))?', re.IGNORECASE)
_MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')


def _source_numbers(text):
    """(scaled, raw): scaled are real values; raw also keeps the digits before a scale word."""
    scaled, raw = set(), set()
    for match in _SRC_NUMBER.finditer(text):
        value = float(match.group(1).replace(',', '') + ('.' + match.group(2) if match.group(2) else ''))
        scale = match.group(3)
        raw.add(value)
        unit = _EN_SCALE.get(scale.lower()) if scale else None
        # Single letters (k/m/b) only scale when glued to the number ("$5m", "3B").
        if unit and len(scale) == 1 and match.group(0)[-2:-1].isspace():
            unit = None
        scaled.add(value * unit if unit else value)
    for match in _WORD_NUMBER.finditer(text):
        word, scale = match.group(1).lower(), match.group(2)
        if word in {'a', 'an'} and not scale:
            continue
        unit = {'hundred': 100, 'decade': 10, 'decades': 10}.get(scale.lower()) if scale else 1
        value = _WORDS[word] * (unit or _EN_SCALE[scale.lower()])
        scaled.add(float(value))
        raw.add(float(_WORDS[word]))
    for match in re.finditer(r'(?<![\d.])(\d+)\s+decades?\b', text, re.IGNORECASE):
        scaled.add(float(match.group(1)) * 10)
    # "a quarter point" is 25 basis points (0.25 percentage points); "half a point" 50.
    if re.search(r'quarter[\s-]+(?:of\s+a\s+)?(?:percentage[\s-]+)?point', text, re.IGNORECASE):
        scaled |= {25.0, 0.25}
    if re.search(r'half[\s-]+(?:a[\s-]+)?(?:percentage[\s-]+)?point', text, re.IGNORECASE):
        scaled |= {50.0, 0.5}
    return scaled, raw


def _matches(value, step, candidates):
    """True when some candidate rounds to `value` at the output's precision `step`."""
    for candidate in candidates:
        if candidate == value:
            return True
        if step and math.isclose(round(candidate / step) * step, value, rel_tol=1e-9, abs_tol=step * 1e-6):
            return True
    return False


def _month_ok(month, source_lower, published):
    return (published is not None and published.month == month) or (
        1 <= month <= 12 and re.search(r'\b' + _MONTHS[month - 1], source_lower) is not None)


def _published(published_at):
    if isinstance(published_at, (int, float)) and math.isfinite(published_at):
        return datetime.datetime.fromtimestamp(published_at, datetime.UTC)
    if isinstance(published_at, str) and published_at:
        try:
            return datetime.datetime.fromisoformat(published_at)
        except ValueError:
            return None
    return None


def unsupported_numbers(output, source, *, published_at=None):
    """Numbers in `output` (Chinese digest text) that the source cannot account for."""
    scaled, raw = _source_numbers(source)
    source_lower = source.lower()
    published = _published(published_at)
    if published is not None:
        # The model sees published_at, so its date parts are evidence (UTC and the day before).
        earlier = published - datetime.timedelta(hours=12)
        raw |= {float(published.year), float(published.month), float(published.day),
                float(earlier.month), float(earlier.day)}
        # "last year" / "next year" resolved against the publication year.
        if re.search(r'\blast year\b', source_lower):
            raw.add(float(published.year - 1))
        if re.search(r'\bnext year\b', source_lower):
            raw.add(float(published.year + 1))
    missing = []
    for match in _OUT_NUMBER.finditer(output):
        integer, decimals, scale = match.group(1).replace(',', ''), match.group(2) or '', match.group(3)
        value = float(integer + ('.' + decimals if decimals else ''))
        after = output[match.end():match.end() + 1]
        if not scale and after == '月' and _month_ok(int(value), source_lower, published):
            continue
        if not scale and after == '年' and not decimals and published and int(value) == published.year:
            continue
        unit = _CN_SCALE.get(scale, 1)
        step = unit * 10 ** -len(decimals)
        if scale:
            # 1.5亿 is a scaled value: compare it with real values only, never raw digits.
            ok = _matches(value * unit, step, scaled)
        else:
            ok = _matches(value, step, scaled | raw)
        if not ok:
            missing.append(match.group(0).strip())
    return missing


# ---- Latin names ---------------------------------------------------------------------

def _fold(value):
    value = unicodedata.normalize('NFKD', value)
    return ''.join(c for c in value if not unicodedata.combining(c)).replace('’', "'").casefold()


def _in_source(word, folded_source):
    """Whole-word, case/accent-insensitive; possessives ("Gantz's") and hyphens are fine.

    Words of 5+ letters also match a source word differing by one trailing letter
    (Zelenskyy/Zelensky, Ukrainian/Ukraine is not that close and still fails).
    """
    word = _fold(word).strip("'-")
    if not word:
        return True
    if re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])', folded_source):
        return True
    if len(word) >= 5:
        stem = re.escape(word[:-1])
        return re.search(r'(?<![a-z])' + stem + r'[a-z]?(?![a-z])', folded_source) is not None
    return False


def _identifying(words):
    """Name parts that identify a person: no titles, particles, numerals or acronyms."""
    return [w for w in words if len(w.strip("'’-")) >= 2 and w.casefold().strip("'’-") not in _NON_IDENTIFYING
            and not (w.isupper() and len(w) <= 6)]


def unsupported_names(output, source):
    """Latin-script names in `output` that never occur in the source."""
    folded = _fold(source)
    missing = []
    # Person brackets: pass when any identifying part (usually the surname) is in the source.
    for match in _BRACKET.finditer(output):
        parts = _identifying(re.split(r"[\s\-]+", match.group(1)))
        if parts and not any(_in_source(part, folded) for part in parts):
            missing.append(match.group(1))
    # Other Latin words: only proper-noun-like ones (capitalised, mixed case, 4+ letters);
    # acronyms (AI, GDP, NHL) and short words are too often added legitimately.
    rest = _BRACKET.sub(' ', output)
    for word in _LATIN_WORD.findall(rest):
        for part in re.split(r'-', word):
            core = part.strip("'’")
            if (len(core) >= 4 and core[0].isupper() and not core.isupper()
                    and core.casefold() not in _NON_IDENTIFYING and not _in_source(core, folded)):
                missing.append(core)
    return missing


def unsupported_glossary(output, source):
    """Glossary people named in Chinese whose Latin name is absent from the source."""
    folded = _fold(source)
    missing = []
    for chinese, latin in GLOSSARY:
        hit = next((name for name in chinese if name in output), None)
        if hit and not any(_in_source(name, folded) for name in latin):
            missing.append(hit)
    return missing


# ---- per item ------------------------------------------------------------------------

def source_text(item):
    """Everything the model saw for this candidate (a superset errs toward passing).

    Uses the bound evidence, never `title`/`summary`: discovery overwrites those with the
    model's Chinese text.
    """
    evidence = item.get('_evidence') or {}
    parts = [item.get('rss_summary'), item.get('content'),
             evidence.get('title'), evidence.get('rss_summary'), evidence.get('content'),
             item.get('publisher'), evidence.get('publisher'), item.get('source'), item.get('category')]
    seen, text = set(), []
    for part in parts:
        if isinstance(part, str) and part and part not in seen:
            seen.add(part)
            text.append(part)
    return '\n'.join(text)


def check_text(output, source, *, published_at=None):
    """Reasons (short strings) this output is not supported by `source`; empty means pass."""
    reasons = []
    reasons += [f'数字:{n}' for n in unsupported_numbers(output, source, published_at=published_at)]
    reasons += [f'人名:{n}' for n in unsupported_names(output, source)]
    reasons += [f'译名:{n}' for n in unsupported_glossary(output, source)]
    return reasons


def check_item(item, fields):
    evidence = item.get('_evidence') or {}
    published = evidence.get('published_at', item.get('published_at'))
    output = '\n'.join(str(item.get(field) or '') for field in fields)
    return check_text(output, source_text(item), published_at=published)


def _clip(value, limit):
    value = ' '.join(str(value or '').split())
    return value if len(value) <= limit else value[:max(1, limit - 1)].rstrip() + '…'


def original_title(item):
    evidence = item.get('_evidence') or {}
    return ' '.join(str(evidence.get('title') or item.get('title') or '').split())


def original_summary(item):
    evidence = item.get('_evidence') or {}
    for value in (evidence.get('rss_summary'), item.get('rss_summary'), item.get('content'), evidence.get('content')):
        text = ' '.join(str(value or '').split())
        if text:
            return text
    return ''


def fallback_fields(item, *, budget):
    """(title, summary) from the source, marked 「原文」, together at most `budget` chars.

    The budget equals the Chinese title + summary limits, so render capacity holds.
    """
    title = _clip(ORIGINAL_MARK + original_title(item), max(len(ORIGINAL_MARK) + 10, budget * 2 // 3))
    summary = _clip(original_summary(item), max(0, budget - len(title)))
    if summary == title[len(ORIGINAL_MARK):] or not summary:
        summary = '（无摘要）'
    return title, summary


def guard(selected, *, fields, title_field, summary_field, budget, clear=()):
    """Check each item; replace failed items' text with the original. Never drops items.

    Each item gets `_factcheck` = {'ok': bool, 'reasons': [...]} for later analysis.
    """
    result = []
    for item in selected:
        reasons = check_item(item, fields)
        item = {**item, '_factcheck': {'ok': not reasons, 'reasons': reasons[:8]}}
        if reasons:
            title, summary = fallback_fields(item, budget=budget)
            item[title_field], item[summary_field] = title, summary
            for field in clear:
                item[field] = ''
        result.append(item)
    return result


_BRACKET_ALLOWANCE = 30


def display_length(value):
    """Length for model-text limits, not counting up to 30 chars of person-name brackets."""
    bracketed = sum(len(m.group(0)) for m in _BRACKET.finditer(value))
    return len(value) - min(_BRACKET_ALLOWANCE, bracketed)


def parse_attribution(attribution):
    """'Provider (model)' → (provider, model); unknown shapes keep the whole string as provider."""
    text = str(attribution or '').strip()
    match = re.fullmatch(r'(.+?)\s*\((.+)\)', text)
    return (match.group(1), match.group(2)) if match else (text, '')
