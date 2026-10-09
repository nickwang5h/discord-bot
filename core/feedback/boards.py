"""Boards: the owner's five interest areas, used to group feedback statistics.

`board_of` is pure. Personal sources carry their section in `FeedSource.category`
(`core.news.personal.SECTIONS`); shared sources are mapped by their category only for
statistics, never to change shared selection.
"""
BOARDS = ('ottawa', 'sudbury', 'invest', 'ai_tech', 'general')
LABELS = {'ottawa': 'Ottawa', 'sudbury': 'Sudbury', 'invest': '投资', 'ai_tech': 'AI 和科技', 'general': 'general'}

# Keys are `core.news.personal.SECTIONS`; a test keeps them in step.
SECTION_BOARDS = {'Ottawa': 'ottawa', 'Sudbury': 'sudbury', 'Investing': 'invest',
                  'AI-Tech': 'ai_tech', 'General': 'general'}
SHARED_BOARDS = {'Finance': 'invest', 'Tech': 'ai_tech', 'AI': 'ai_tech'}


def board_of(source, category):
    """Board id for a feed source. `source` (FeedSource.name) is accepted for a stable
    signature; today the category alone decides. Unknown categories fall to `general`."""
    if category in BOARDS:
        return category
    return SECTION_BOARDS.get(category) or SHARED_BOARDS.get(category) or 'general'


def source_catalog():
    """Every configured source as `{name, category, board, personal}`; re-reads the personal list."""
    from core.news import sources

    catalog = {}
    for group in sources.SHARED_GROUPS.values():
        for source in group:
            catalog.setdefault(source.name, {'name': source.name, 'category': source.category,
                                             'board': board_of(source.name, source.category), 'personal': False})
    entries, _ = sources.personal_entries()
    for entry in entries:
        catalog.setdefault(entry['name'], {'name': entry['name'], 'category': entry['category'],
                                           'board': board_of(entry['name'], entry['category']), 'personal': True})
    return list(catalog.values())
