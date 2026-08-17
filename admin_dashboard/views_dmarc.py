"""
DMARC aggregate report dashboard.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names, so urls.py — which references them as
`views.<name>` — keeps working unchanged.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from django.conf import settings
import datetime


# ────────────────────────────────────────────────────────────────────────────
# DMARC aggregate report dashboard
# ────────────────────────────────────────────────────────────────────────────

def _format_seconds(s):
    """3600 → '1h', 90061 → '1d 1h', 45 → '45s'. Used by redis_monitor."""
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m'
    if s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return f'{h}h {m}m' if m else f'{h}h'
    d = s // 86400
    h = (s % 86400) // 3600
    return f'{d}d {h}h' if h else f'{d}d'


@admin_required
def redis_monitor(request):
    """
    Operational dashboard for Redis client connections. Three views in
    one page:

      1. Right-now snapshot — pulls CLIENT LIST live and buckets by
         process category derived from CLIENT SETNAME.
      2. Last 24 hours — peak total per hour from the 5-min snapshot
         feed, rendered as a CSS bar chart.
      3. Recent snapshots — last 50 rows of the snapshot table for
         spot-checking exact numbers.

    Live snapshot is best-effort. If Redis is down or unreachable the
    page still renders the historical chart from the DB.
    """
    from collections import defaultdict
    from reporting.models import RedisConnectionSnapshot
    from reporting.tasks import _categorize_client_name

    # ── 1. Right-now snapshot ───────────────────────────────────────
    live_total = 0
    live_categories = {}
    live_clients = []
    live_error = ''
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL)
        raw = r.execute_command('CLIENT', 'LIST')
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8', 'replace')
        lines = [line for line in raw.splitlines() if line.strip()]
        live_total = len(lines)
        cat_counts = defaultdict(int)
        for line in lines:
            name = ''
            age = idle = 0
            for field in line.split(' '):
                if field.startswith('name='):
                    name = field[5:]
                elif field.startswith('age='):
                    try:
                        age = int(field[4:])
                    except ValueError:
                        pass
                elif field.startswith('idle='):
                    try:
                        idle = int(field[5:])
                    except ValueError:
                        pass
            bucket = _categorize_client_name(name)
            cat_counts[bucket] += 1
            live_clients.append({
                'name': name or '(unset)',
                'category': bucket,
                'age': age,
                'idle': idle,
                'age_display': _format_seconds(age),
                'idle_display': _format_seconds(idle),
                # Flagged when a single connection has been around for
                # > 1 day — the case the operator specifically asked
                # about ("any used for days?"). UI highlights the row.
                'long_lived': age >= 86400,
            })
        live_categories = dict(cat_counts)
        # Sort: oldest first — long-lived connections at the top
        live_clients.sort(key=lambda c: c['age'], reverse=True)
    except Exception as exc:  # noqa: BLE001
        live_error = str(exc)

    # ── 2. Last 24 hours — hourly peaks ─────────────────────────────
    cutoff = timezone.now() - datetime.timedelta(hours=24)
    snapshots_24h = list(
        RedisConnectionSnapshot.objects
        .filter(captured_at__gte=cutoff)
        .order_by('captured_at')
    )
    hourly = defaultdict(int)
    for s in snapshots_24h:
        # Bucket by hour-of-day (local time)
        bucket_dt = timezone.localtime(s.captured_at).replace(
            minute=0, second=0, microsecond=0)
        hourly[bucket_dt] = max(hourly[bucket_dt], s.total)
    # Build 24 contiguous buckets ending at the current hour
    now_hour = timezone.localtime().replace(
        minute=0, second=0, microsecond=0)
    chart = []
    chart_max = max(list(hourly.values()) + [1])
    for h_offset in range(23, -1, -1):
        slot = now_hour - datetime.timedelta(hours=h_offset)
        peak = hourly.get(slot, 0)
        chart.append({
            'hour': slot,
            'peak': peak,
            'pct': round(peak * 100 / chart_max) if chart_max else 0,
            'is_now': h_offset == 0,
        })

    # ── 3. Recent snapshots table ───────────────────────────────────
    recent = list(
        RedisConnectionSnapshot.objects
        .order_by('-captured_at')[:50]
    )

    # Sparkline-style: last hour as 12 5-min buckets so a recent spike
    # is visible even when 24h chart's hourly granularity smooths it
    recent_5min = [s for s in snapshots_24h if (
        s.captured_at >= timezone.now() - datetime.timedelta(hours=1))][-12:]

    return render(request, 'admin_dashboard/redis_monitor.html',
                  _admin_context(
                      active='redis_monitor',
                      live_total=live_total,
                      live_categories=live_categories,
                      live_clients=live_clients,
                      live_error=live_error,
                      chart=chart,
                      chart_max=chart_max,
                      recent=recent,
                      recent_5min=recent_5min,
                      snapshot_count=len(snapshots_24h),
                  ))


@admin_required
def dmarc_dashboard(request):
    """
    DMARC aggregate report overview at /admin-dashboard/dmarc/.

    Shows:
      - 30-day pass-rate trend (one bar per day)
      - Per-reporter breakdown (Gmail / Microsoft / Yahoo / etc.)
      - Top failing source IPs (likely spoofers + misconfigured
        third-party senders you forgot about)
      - Recent reports table
      - Manual upload form (paste a .zip / .gz / .xml from an
        email attachment — useful before the IMAP poller is wired)
    """
    from datetime import timedelta

    from django.db.models import Count, Sum
    from django.utils import timezone as dj_tz

    from reporting.models import DmarcRecord, DmarcReport

    # Window is adjustable via ?days= so a backfill of older reports is
    # actually visible. With the window hard-coded to 30 days, ingesting
    # an older report succeeded but rendered nothing — indistinguishable
    # from the ingest being broken.
    try:
        days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))

    # tz-aware cutoff. period_end is a UTC datetime; comparing it to a
    # naive date() triggered a naive-datetime warning and put the window
    # boundary a few hours off the intended point.
    cutoff = dj_tz.now() - timedelta(days=days)

    qs = DmarcReport.objects.filter(period_end__gte=cutoff)

    # Totals across the 30-day window.
    totals = qs.aggregate(
        total_msgs=Sum('total_messages'),
        total_pass=Sum('dmarc_pass'),
        total_fail=Sum('dmarc_fail'),
        total_reports=Count('id'),
    )
    total_msgs = totals['total_msgs'] or 0
    total_pass = totals['total_pass'] or 0
    total_fail = totals['total_fail'] or 0
    pass_rate = (
        100.0 * total_pass / total_msgs) if total_msgs else 0.0

    # ── 30-day daily trend (bar chart) ──
    # Bucket by period_end date so each report lands on a single
    # day. Simple in Python — ≤ ~60 reports per month even for
    # high-volume domains.
    by_day = {}
    for r in qs:
        # localtime() so bucket keys match the localdate() strip below —
        # period_end is UTC, the strip was server-local, and the two
        # disagreed for reports landing near midnight.
        d = dj_tz.localtime(r.period_end).date()
        bucket = by_day.setdefault(
            d, {'date': d, 'pass': 0, 'fail': 0, 'total': 0})
        bucket['pass'] += r.dmarc_pass
        bucket['fail'] += r.dmarc_fail
        bucket['total'] += r.total_messages

    # Build the strip, grouping days into buckets so the column count
    # stays readable at any window.
    #
    # It used to be `min(days, 90)` daily columns. At the 1-year window
    # that is 90 columns which, at the 18px minimum each, is ~2000px —
    # wider than the card, so the chart overflowed the page. Grouping
    # keeps roughly 30-52 columns whatever the window:
    #
    #   ≤ 45 days   → one column per day
    #   ≤ 120 days  → one per week   (90d  → ~13 columns)
    #   otherwise   → one per month  (365d → 12 columns)
    if days <= 45:
        group_days, group_label = 1, 'day'
    elif days <= 120:
        group_days, group_label = 7, 'week'
    else:
        group_days, group_label = 30, 'month'

    trend_days = days
    n_buckets = max(1, -(-days // group_days))   # ceil
    trend = []
    today = dj_tz.localdate()

    for b in range(n_buckets - 1, -1, -1):
        end = today - timedelta(days=b * group_days)
        start = end - timedelta(days=group_days - 1)
        agg = {'pass': 0, 'fail': 0, 'total': 0}
        for offset in range(group_days):
            d = start + timedelta(days=offset)
            day = by_day.get(d)
            if day:
                agg['pass'] += day['pass']
                agg['fail'] += day['fail']
                agg['total'] += day['total']
        # Portable label — %-m/%-d works on Linux/Mac but not Windows,
        # so build it from .month / .day and don't 500 dev boxes.
        label = f'{end.month}/{end.day}'
        trend.append({
            'date': end,
            'start': start,
            'date_short': label,
            'pass': agg['pass'],
            'fail': agg['fail'],
            'total': agg['total'],
        })

    # Bar height is proportional to VOLUME, measured against the
    # busiest bucket — which is what the chart always claimed to show
    # and never did. Previously each bar was a 100%-stacked pass/fail
    # split, so a day with 4 messages looked exactly like a day with
    # 400 and the shape of the traffic was invisible.
    #
    # The pass/fail split within a bar is now expressed with flex-grow
    # rather than percentage heights (see the template), so it needs no
    # percentage to resolve against and cannot collapse.
    busiest = max((t['total'] for t in trend), default=0) or 1
    for t in trend:
        if t['total']:
            # Floor at 4% so a genuinely quiet bucket is still a
            # visible mark rather than indistinguishable from a gap.
            t['height_pct'] = max(4, round(100 * t['total'] / busiest))
        else:
            t['height_pct'] = 0

    # ── Reporters breakdown ──
    by_org = (qs.values('org_name')
              .annotate(
                  reports=Count('id'),
                  msgs=Sum('total_messages'),
                  pass_=Sum('dmarc_pass'),
                  fail=Sum('dmarc_fail'))
              .order_by('-msgs'))
    reporters = []
    for o in by_org:
        msgs = o['msgs'] or 0
        reporters.append({
            'org_name': o['org_name'],
            'reports': o['reports'],
            'msgs': msgs,
            'pass': o['pass_'] or 0,
            'fail': o['fail'] or 0,
            'pass_rate': (100.0 * (o['pass_'] or 0) / msgs) if msgs else 0,
        })

    # ── Top failing source IPs (in the 30-day window) ──
    failing = (
        DmarcRecord.objects
        .filter(report__period_end__gte=cutoff)
        .filter(dkim_aligned__in=('fail', 'none'),
                spf_aligned__in=('fail', 'none'))
        .values('source_ip')
        .annotate(msgs=Sum('count'), seen=Count('id'))
        .order_by('-msgs')[:10]
    )
    top_failing = list(failing)

    # ── Recent reports table ──
    # Deliberately NOT window-filtered. This table answers "did anything
    # land at all?", so it must show the newest reports even when they
    # fall outside the selected window (e.g. right after a backfill of
    # older reports).
    recent_reports = list(
        DmarcReport.objects.order_by('-received_at')[:25])

    return render(request, 'admin_dashboard/dmarc.html', _admin_context(
        active='dmarc',
        total_msgs=total_msgs,
        total_pass=total_pass,
        total_fail=total_fail,
        pass_rate=pass_rate,
        total_reports=totals['total_reports'] or 0,
        trend=trend,
        trend_days=trend_days,
        # 'day' | 'week' | 'month' — the template says which, so a
        # 12-bar year chart cannot be mistaken for 12 days.
        trend_group=group_label,
        window_days=days,
        # Lets the template tell "nothing ever ingested" apart from
        # "nothing in the selected window" — two very different problems
        # that used to render the same empty state.
        has_any_reports=bool(recent_reports),
        reporters=reporters,
        top_failing=top_failing,
        recent_reports=recent_reports,
    ))


@admin_required
@require_POST
def dmarc_upload(request):
    """
    Manual DMARC report upload. Accepts .zip / .gz / .xml — same
    format every provider sends. The parser sniffs magic bytes so
    the extension just helps with debugging.

    Used:
      - For initial backfill (forward old reports from Gmail).
      - Whenever you want to ingest a one-off report.
      - When the IMAP poller isn't running yet (it's opt-in).
    """
    from django.contrib import messages

    from reporting.dmarc import (
        ingest_dmarc_xml, parse_dmarc_attachment,
    )

    uploaded = request.FILES.get('report')
    if not uploaded:
        messages.error(request, 'No file uploaded.')
        return redirect('admin_dashboard:dmarc_dashboard')

    raw = uploaded.read()
    xml = parse_dmarc_attachment(raw, filename=uploaded.name)
    if not xml:
        messages.error(
            request,
            f'Could not extract XML from "{uploaded.name}" — '
            f'expected .zip / .gz / .xml.')
        return redirect('admin_dashboard:dmarc_dashboard')

    report = ingest_dmarc_xml(xml)
    if report is None:
        messages.error(
            request,
            f'"{uploaded.name}" parsed but ingest failed (malformed XML '
            f'or duplicate report_id). Check the server logs.')
    else:
        # Detect duplicate vs fresh — fresh reports have received_at
        # within the last few seconds.
        from django.utils import timezone as _tz
        from datetime import timedelta as _td
        is_fresh = (_tz.now() - report.received_at) < _td(seconds=10)
        if is_fresh:
            messages.success(
                request,
                f'Ingested report from {report.org_name} — '
                f'{report.total_messages} messages '
                f'({report.dmarc_pass} pass / {report.dmarc_fail} fail).')
        else:
            messages.info(
                request,
                f'Report already in database — uploaded by '
                f'{report.received_at:%Y-%m-%d %H:%M}. No changes.')
    return redirect('admin_dashboard:dmarc_dashboard')


