"""Keyword-ranking display helpers — shared by the admin and portal views."""


def position_class(position):
    """A CSS-class suffix for a ranking position's colour band."""
    if position is None:
        return 'muted'
    if position <= 3:
        return 'top'
    if position <= 10:
        return 'page1'
    if position <= 20:
        return 'page2'
    return 'low'


def keyword_trend(current, previous):
    """
    Trend descriptor between two rank records.
    Returns {symbol, label, css}. Lower position numbers are better.
    """
    cur_pos = current.position if current else None
    prev_pos = previous.position if previous else None

    if cur_pos is None:
        return {'symbol': '—', 'label': 'Not ranked', 'css': 'muted'}
    if previous is None or prev_pos is None:
        return {'symbol': '★', 'label': 'New', 'css': 'new'}

    gained = prev_pos - cur_pos
    if gained > 0:
        return {'symbol': '↑', 'label': f'Up {gained}', 'css': 'up'}
    if gained < 0:
        return {'symbol': '↓', 'label': f'Down {-gained}', 'css': 'down'}
    return {'symbol': '→', 'label': 'No change', 'css': 'same'}


def build_keyword_rows(client, active_only=False):
    """Per-keyword display rows: current/previous record, trend, colour band."""
    keywords = client.tracked_keywords.all()
    if active_only:
        keywords = keywords.filter(is_active=True)

    rows = []
    for kw in keywords:
        records = list(kw.rank_records.all()[:2])  # newest first
        current = records[0] if records else None
        previous = records[1] if len(records) > 1 else None
        position = current.position if current else None
        rows.append({
            'keyword': kw,
            'current': current,
            'previous': previous,
            'position': position,
            'position_class': position_class(position),
            'trend': keyword_trend(current, previous),
            'impressions': current.impressions if current else 0,
            'clicks': current.clicks if current else 0,
        })
    return rows


def keyword_insight(rows):
    """A one-line plain-English summary of a set of keyword rows."""
    if not rows:
        return ''
    page1 = sum(1 for r in rows if r['position'] and r['position'] <= 10)
    improved = sum(1 for r in rows if r['trend']['css'] == 'up')
    return (
        f'You are ranking on page 1 for {page1} '
        f'keyword{"" if page1 == 1 else "s"}. '
        f'{improved} keyword{"" if improved == 1 else "s"} '
        f'improved this month.'
    )
