"""
Tier 1 page-session analytics helpers.

Backed by the `PageSession` table (one row per page view) populated
by the v2 aspired-tracker.js beacon. All functions take a
`ClientProfile` and a window (defaults to 30 days) and return
serializable dicts — safe to feed straight into templates or JSON.
"""

from collections import Counter
from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from .models import PageSession


DEFAULT_WINDOW_DAYS = 30


def _qs(client, days=DEFAULT_WINDOW_DAYS):
    since = timezone.now() - timedelta(days=days)
    return PageSession.objects.filter(
        client=client, created_at__gte=since)


def _band_time(seconds):
    """Time-on-page colour band — green/orange/red."""
    if seconds is None:
        return 'muted'
    if seconds > 120:
        return 'good'
    if seconds >= 60:
        return 'ok'
    return 'poor'


def _band_scroll(pct):
    """Scroll-depth colour band — green/orange/red."""
    if pct is None:
        return 'muted'
    if pct > 75:
        return 'good'
    if pct >= 50:
        return 'ok'
    return 'poor'


def overview_stats(client, days=DEFAULT_WINDOW_DAYS):
    """Top-row summary card data for the conversion dashboard."""
    qs = _qs(client, days)
    total = qs.count()
    if not total:
        return {
            'days': days,
            'total_page_views': 0,
            'avg_time_on_page_seconds': None,
            'avg_time_on_page_display': '—',
            'avg_time_band': 'muted',
            'avg_scroll_depth': None,
            'avg_scroll_band': 'muted',
            'exit_intent_rate': 0,
            'exit_intent_count': 0,
        }

    # Use Count(filter=Q(...)) for the boolean rather than
    # Sum('exit_intent_fired') — SQLite happily SUM()s booleans,
    # but PostgreSQL refuses ("function sum(boolean) does not
    # exist"), so the Sum version 500'd on prod even though tests
    # passed locally.
    agg = qs.aggregate(
        avg_time=Avg('time_on_page_seconds'),
        avg_scroll=Avg('max_scroll_depth'),
        exits=Count('id', filter=Q(exit_intent_fired=True)),
    )

    avg_time = (int(round(agg['avg_time']))
                if agg['avg_time'] is not None else None)
    avg_scroll = (int(round(agg['avg_scroll']))
                  if agg['avg_scroll'] is not None else None)
    exit_count = int(agg['exits'] or 0)
    exit_rate = int(round((exit_count / total) * 100)) if total else 0

    return {
        'days': days,
        'total_page_views': total,
        'avg_time_on_page_seconds': avg_time,
        'avg_time_on_page_display': _format_seconds(avg_time),
        'avg_time_band': _band_time(avg_time),
        'avg_scroll_depth': avg_scroll,
        'avg_scroll_band': _band_scroll(avg_scroll),
        'exit_intent_rate': exit_rate,
        'exit_intent_count': exit_count,
    }


def _format_seconds(s):
    if s is None:
        return '—'
    if s < 60:
        return f'{s}s'
    m = s // 60
    sec = s % 60
    return f'{m}m {sec}s'


def conversion_funnel(client, days=DEFAULT_WINDOW_DAYS):
    """
    Page Views → Engaged → CTA Clicks → Form Submits, with
    pct-of-previous-step and pct-of-total. "Engaged" = sessions
    with time > 60s OR scroll > 50%.
    """
    qs = _qs(client, days)
    total = qs.count()
    if not total:
        return {
            'page_views': 0, 'engaged': 0, 'engaged_pct': 0,
            'cta_clicks': 0, 'cta_pct': 0,
            'form_submits': 0, 'form_pct': 0,
        }

    from django.db.models import Q
    engaged = qs.filter(
        Q(time_on_page_seconds__gt=60)
        | Q(max_scroll_depth__gt=50)).count()

    cta = qs.aggregate(s=Sum('cta_clicks'))['s'] or 0
    forms = qs.aggregate(s=Sum('form_submits'))['s'] or 0

    def _pct(n, base):
        return int(round((n / base) * 100)) if base else 0

    return {
        'page_views': total,
        'engaged': engaged,
        'engaged_pct': _pct(engaged, total),
        'cta_clicks': cta,
        'cta_pct': _pct(cta, total),
        'form_submits': forms,
        'form_pct': _pct(forms, total),
    }


def top_pages(client, days=DEFAULT_WINDOW_DAYS, limit=10):
    """Top pages by view count with aggregate engagement metrics."""
    qs = _qs(client, days)
    if not qs.exists():
        return []

    rows = (
        qs.values('page_url')
        .annotate(
            views=Count('id'),
            avg_time=Avg('time_on_page_seconds'),
            avg_scroll=Avg('max_scroll_depth'),
            cta=Sum('cta_clicks'),
            forms=Sum('form_submits'),
            phones=Sum('phone_clicks'),
        )
        .order_by('-views')[:limit]
    )

    out = []
    for r in rows:
        time_s = (int(round(r['avg_time']))
                  if r['avg_time'] is not None else None)
        scroll = (int(round(r['avg_scroll']))
                  if r['avg_scroll'] is not None else None)
        out.append({
            'page_url': r['page_url'] or '(unknown)',
            'views': r['views'],
            'avg_time_seconds': time_s,
            'avg_time_display': _format_seconds(time_s),
            'avg_scroll_depth': scroll,
            'conversions': (r['cta'] or 0) + (r['forms'] or 0)
                           + (r['phones'] or 0),
        })
    return out


def scroll_distribution(client, days=DEFAULT_WINDOW_DAYS):
    """
    4-bucket distribution of max_scroll_depth, with the colour band
    each bucket reads as.
    """
    qs = _qs(client, days).exclude(max_scroll_depth__isnull=True)
    total = qs.count()

    buckets = [
        ('0-25%',   0,  25,  'poor'),
        ('25-50%',  25, 50,  'ok'),
        ('50-75%',  50, 75,  'teal'),
        ('75-100%', 75, 101, 'good'),
    ]
    out = []
    for label, lo, hi, band in buckets:
        if lo == 0:
            n = qs.filter(max_scroll_depth__lt=hi).count()
        elif hi == 101:
            n = qs.filter(max_scroll_depth__gte=lo).count()
        else:
            n = qs.filter(
                max_scroll_depth__gte=lo,
                max_scroll_depth__lt=hi).count()
        pct = int(round((n / total) * 100)) if total else 0
        out.append({
            'label': label,
            'count': n,
            'pct': pct,
            'band': band,
        })
    return out


def click_heatmap_grid(client, days=DEFAULT_WINDOW_DAYS, grid=10):
    """
    Aggregate every click in the window into a `grid` × `grid`
    density matrix. Returns list-of-list-of-dicts so the template
    can iterate rows then cells without arithmetic.

    Kept for backwards-compat with anything still calling it —
    the conversions page now uses click_breakdown() instead.
    """
    qs = _qs(client, days)
    cells = Counter()
    for session in qs.only('click_heatmap'):
        for click in (session.click_heatmap or []):
            try:
                x = int(click.get('x_pct', 0))
                y = int(click.get('y_pct', 0))
            except (TypeError, ValueError):
                continue
            col = min(grid - 1, max(0, x * grid // 100))
            row = min(grid - 1, max(0, y * grid // 100))
            cells[(row, col)] += 1

    def _band(n):
        if n == 0:    return 'b0'
        if n <= 2:    return 'b1'
        if n <= 5:    return 'b2'
        if n <= 10:   return 'b3'
        return 'b4'

    out = []
    for row in range(grid):
        out_row = []
        for col in range(grid):
            n = cells.get((row, col), 0)
            out_row.append({'count': n, 'band': _band(n)})
        out.append(out_row)
    return out


# ── Click breakdown (section bar chart + canvas overlay) ──────────────────

# Page sections by vertical position (y_pct).
_SECTIONS = (
    ('Header / Nav',     0, 15),
    ('Hero',            15, 30),
    ('Upper Content',   30, 50),
    ('Middle Content',  50, 70),
    ('Lower Content',   70, 85),
    ('Footer',          85, 101),
)


def click_breakdown(client, days=DEFAULT_WINDOW_DAYS,
                    overlay_cap=500):
    """
    Three pieces of click-heatmap data for the conversions page:

        sections       list of dicts:
                       [{label, count, pct, total}]
        overlay_clicks list of {x_pct, y_pct} for the canvas
                       (capped at `overlay_cap` to keep payload sane)
        top_elements   list of (text, count) tuples — top 10 most-
                       clicked element texts across the window

    All single-pass over the session set — one query.
    """
    qs = _qs(client, days).only('click_heatmap')

    section_counts = Counter()
    overlay = []
    element_texts = []

    for session in qs:
        for click in (session.click_heatmap or []):
            try:
                y = int(click.get('y_pct', 0))
                x = int(click.get('x_pct', 0))
            except (TypeError, ValueError):
                continue
            y = max(0, min(100, y))
            x = max(0, min(100, x))

            # Section by y_pct.
            for label, lo, hi in _SECTIONS:
                if lo <= y < hi:
                    section_counts[label] += 1
                    break

            # Overlay coordinate (only stash up to overlay_cap).
            if len(overlay) < overlay_cap:
                overlay.append({'x_pct': x, 'y_pct': y})

            # Element text for the top-clicked list.
            text = (click.get('text') or '').strip()
            if text and len(text) > 2:
                element_texts.append(text[:80])

    total = sum(section_counts.values())
    sections = []
    for label, _lo, _hi in _SECTIONS:
        n = section_counts.get(label, 0)
        pct = int(round((n / total) * 100)) if total else 0
        sections.append({
            'label': label, 'count': n, 'pct': pct, 'total': total,
        })

    top_elements = Counter(element_texts).most_common(10)

    return {
        'sections': sections,
        'overlay_clicks': overlay,
        'top_elements': top_elements,
        'total_clicks': total,
    }


def recent_sessions(client, limit=50):
    """Latest session rows for the table at the bottom of the page."""
    return (PageSession.objects
            .filter(client=client)
            .order_by('-created_at')[:limit])


# ── Portal-only insights (plain-English copy) ─────────────────────────────

def scroll_insight(avg_pct):
    """Plain-English line about average scroll depth."""
    if avg_pct is None:
        return ('We haven\'t recorded enough page views yet. '
                'Visitor data will appear here as your site gets '
                'traffic.')
    if avg_pct < 40:
        return ('Most visitors leave before reading half your page. '
                'Your most important content should be higher up.')
    if avg_pct < 70:
        return ('Visitors are reading a good portion of your page '
                'before deciding.')
    return ('Visitors are reading most of your page — great '
            'engagement.')


def exit_intent_insight(rate_pct):
    """Plain-English line — only return text if the rate is concerning."""
    if rate_pct is None or rate_pct < 40:
        return ''
    return (f'Over {rate_pct}% of visitors showed signs of leaving. '
            f'A clear call-to-action near the top of the page may '
            f'help capture more interest.')
