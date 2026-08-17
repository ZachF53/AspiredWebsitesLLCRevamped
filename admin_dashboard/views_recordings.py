"""
Session recording (rrweb) admin.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

import json
import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .decorators import admin_required
from .utils import _is_uuid

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Tier 2 — Session recording (rrweb) admin views
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def recordings_list(request, website_id):
    """
    Per-website recordings table with storage stats + filters.

    Filters (all optional, all GET): page, min_duration, q.
    """
    from django.db.models import Avg, Count, Sum
    from django.utils import timezone

    from clients.account_models import Website
    from reporting.models import SessionRecording

    website = get_object_or_404(Website, id=website_id)

    qs = (SessionRecording.objects
          .filter(website_new=website).order_by('-created_at'))

    # Filters.
    page_filter = (request.GET.get('page') or '').strip()
    min_dur = (request.GET.get('min_duration') or '').strip()
    device_filter = (request.GET.get('device') or '').strip()
    if page_filter:
        qs = qs.filter(page_url__icontains=page_filter)
    if min_dur:
        try:
            qs = qs.filter(duration_seconds__gte=int(min_dur))
        except (TypeError, ValueError):
            pass
    # Ignore an unknown device slug rather than returning zero rows.
    valid_devices = {c for c, _ in SessionRecording.device_type.field.choices}
    if device_filter in valid_devices:
        qs = qs.filter(device_type=device_filter)
    else:
        device_filter = ''

    # Storage stats — never filtered, always full picture.
    stats = SessionRecording.objects.filter(website_new=website).aggregate(
        total_recordings=Count('id'),
        total_size_kb=Sum('estimated_size_kb'),
        avg_duration=Avg('duration_seconds'),
    )
    total_size_kb = stats['total_size_kb'] or 0
    total_size_mb = round(total_size_kb / 1024, 1)

    oldest = (SessionRecording.objects
              .filter(website_new=website)
              .order_by('created_at').first())
    oldest_days = (timezone.now() - oldest.created_at).days if oldest else 0

    # Distinct page URLs for the dropdown filter.
    pages_seen = list(
        SessionRecording.objects.filter(website_new=website)
        .values_list('page_url', flat=True)
        .distinct().order_by('page_url')[:30])

    # Device split across all recordings for this site (unfiltered, so
    # the headline number doesn't move when a filter is applied).
    device_counts = {
        row['device_type']: row['n']
        for row in (SessionRecording.objects
                    .filter(website_new=website)
                    .values('device_type')
                    .annotate(n=Count('id')))
    }
    device_labels = dict(SessionRecording.device_type.field.choices)
    device_summary = ' · '.join(
        f'{device_labels.get(k, k)} {v}'
        for k, v in sorted(device_counts.items(), key=lambda kv: -kv[1])
    ) or '—'

    return render(
        request,
        'admin_dashboard/recordings_list.html',
        _admin_context(
            'clients',
            website=website,
            recordings=qs[:200],
            total_recordings=stats['total_recordings'] or 0,
            total_size_mb=total_size_mb,
            oldest_days=oldest_days,
            pages_seen=pages_seen,
            device_choices=SessionRecording.device_type.field.choices,
            device_summary=device_summary,
            filter_page=page_filter,
            filter_min_duration=min_dur,
            filter_device=device_filter,
        ),
    )


@admin_required
def recording_replay(request, website_id, rec_id):
    """
    Full-page rrweb Replayer view. Events are inlined via
    `{{ events_json|json_script:"recording-events" }}` so the
    payload is automatically HTML-escaped inside a typed
    <script type="application/json"> tag (XSS-safe). The replay
    JS parses that with JSON.parse — no |safe filter needed.
    """
    from django.core.serializers.json import DjangoJSONEncoder

    from clients.account_models import Website
    from reporting.models import SessionRecording

    website = get_object_or_404(Website, id=website_id)
    rec = get_object_or_404(SessionRecording, id=rec_id, website_new=website)
    events = rec.get_all_events()

    # Lightweight diagnostics for the operator — surface whether
    # the recording will actually replay before they click Play.
    # rrweb event types: 0=DomContentLoaded, 1=Load, 2=FullSnapshot,
    # 3=IncrementalSnapshot, 4=Meta, 5=Custom.
    first_event_type = (events[0].get('type')
                        if events and isinstance(events[0], dict)
                        else None)
    has_full_snapshot = any(
        isinstance(e, dict) and e.get('type') == 2 for e in events)

    return render(
        request,
        'admin_dashboard/recording_replay.html',
        _admin_context(
            'clients',
            website=website,
            recording=rec,
            # DjangoJSONEncoder handles datetime/UUID/Decimal cleanly
            # if any sneak into the rrweb chunks via custom plugins.
            events_json=json.dumps(events, cls=DjangoJSONEncoder),
            event_count=len(events),
            first_event_type=first_event_type,
            has_full_snapshot=has_full_snapshot,
        ),
    )


@admin_required
def recording_download(request, website_id, rec_id):
    """
    Stream a self-contained HTML file — rrweb-player CSS + JS +
    the recording events all inlined. Recipient just opens it in
    any browser, no server required.
    """
    from pathlib import Path

    from django.http import HttpResponse

    from clients.account_models import Website
    from reporting.models import SessionRecording

    website = get_object_or_404(Website, id=website_id)
    rec = get_object_or_404(SessionRecording, id=rec_id, website_new=website)

    static_root = Path(settings.BASE_DIR) / 'core' / 'static' / 'js'
    try:
        rrweb_js = (static_root / 'rrweb.min.js').read_text(
            encoding='utf-8')
    except OSError:
        rrweb_js = ''

    events = rec.get_all_events()
    events_json = json.dumps(events, default=str)

    safe_page = (rec.page_url or '').replace(
        'https://', '').replace('http://', '').replace('/', '_')[:60]
    safe_page = safe_page or 'page'
    filename = (f'recording-{rec.created_at:%Y%m%d-%H%M}-'
                f'{safe_page}.html')

    html = render(
        request,
        'admin_dashboard/recording_download.html',
        {
            'website': website,
            'recording': rec,
            'rrweb_js': rrweb_js,
            'events_json': events_json,
        },
    ).content

    response = HttpResponse(html, content_type='text/html')
    response['Content-Disposition'] = (
        f'attachment; filename="{filename}"')
    return response


@admin_required
@require_POST
def recording_delete(request, website_id, rec_id):
    """Single-row delete from the recordings list."""
    from django.contrib import messages as _msg

    from clients.account_models import Website
    from reporting.models import SessionRecording

    website = get_object_or_404(Website, id=website_id)
    rec = get_object_or_404(SessionRecording, id=rec_id, website_new=website)
    rec.delete()
    _msg.success(request, 'Recording deleted.')
    return redirect('admin_dashboard:recordings_list',
                    website_id=website.id)


@admin_required
@require_POST
def recording_delete_all(request, website_id):
    """Wipe every recording for one website (with confirmation in template)."""
    from django.contrib import messages as _msg

    from clients.account_models import Website
    from reporting.models import SessionRecording

    website = get_object_or_404(Website, id=website_id)
    n, _ = SessionRecording.objects.filter(website_new=website).delete()
    _msg.success(request, f'Deleted {n} recording(s).')
    return redirect('admin_dashboard:recordings_list',
                    website_id=website.id)


