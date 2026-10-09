"""Deterministic topic labels for items without model tags. No model, no network.

English: runs of 1–4 consecutive capitalised words, minus the sentence-initial word
and stopwords; longer runs are dropped. In a Title Case headline capitals say nothing,
so only acronyms and inner-capital names (OPG, OpenAI) count.
Chinese: text inside 《》, “”, 「」 or "" quotes. At most three labels per title.
"""
import re

MAX_ENTITIES = 3
MAX_RUN = 4
_CJK = re.compile(r'[㐀-鿿]')
_QUOTED = re.compile(r'《([^《》]{1,30})》|“([^“”]{1,30})”|「([^「」]{1,30})」|"([^"]{1,30})"')
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9&'’.\-]*|[^\sA-Za-z]")
_SENTENCE_END = {'.', '?', '!', '？', '！', '。'}
STOPWORDS = frozenset('''
a an and are as at be but by can could did do does for from had has have he her his how i if in into is it
its may might more most my no not of off on or our out over says said she should so than that the their
then there these they this those to under up us was we were what when where which who why will with would
you your after before about amid as against again all also any back best big breaking can't first here just
last latest live now one only opinion analysis exclusive report update updates video watch week today
year years still top two three how's what's here's why's
monday tuesday wednesday thursday friday saturday sunday
january february march april june july august september october november december
'''.split())


def _capitalised(word):
    return word[0].isupper() or (len(word) > 1 and word[1].isupper())


def _title_case(words):
    """Headline Title Case carries no signal: every content word is capitalised."""
    content = [w for w in words if w.casefold() not in STOPWORDS]
    return len(content) >= 3 and all(_capitalised(w) for w in content)


def _distinctive(word):
    """Still a name inside Title Case: an acronym or inner capitals (OpenAI, iPhone)."""
    return (len(word) > 1 and word.isupper()) or any(c.isupper() for c in word[1:])


def _english(title, *, drop_initial):
    found, run = [], []
    title_case = _title_case([t for t in _TOKEN.findall(title) if t[0].isalpha()])

    def close():
        if 1 <= len(run) <= MAX_RUN:
            found.append(' '.join(run))
        run.clear()

    sentence_start = True
    for token in _TOKEN.findall(title):
        if not token[0].isalpha():
            close()
            sentence_start = sentence_start or token in _SENTENCE_END
            continue
        word = token.strip(".'’-")
        initial, sentence_start = sentence_start, False
        distinctive = _distinctive(word)
        if (not word or not _capitalised(word) or (word.casefold() in STOPWORDS and not distinctive)
                or (initial and drop_initial and not distinctive)
                or (title_case and not distinctive)):
            close()
            continue
        run.append(word)
    close()
    return found


def extract_entities(title, limit=MAX_ENTITIES):
    """Up to `limit` labels in order of appearance, case-insensitively unique."""
    title = str(title or '')[:300]
    labels = [next(group for group in match.groups() if group).strip() for match in _QUOTED.finditer(title)]
    # Capitalisation is only a signal in English text; inside Chinese the first word is a real name.
    labels += _english(_QUOTED.sub(' | ', title), drop_initial=not _CJK.search(title))
    result, seen = [], set()
    for label in labels:
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            result.append(label)
        if len(result) == limit:
            break
    return result
