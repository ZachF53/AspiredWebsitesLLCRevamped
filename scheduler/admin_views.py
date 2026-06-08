"""
Admin-dashboard views for managing scheduling — AvailabilityWindow
CRUD + ScheduledCall list/cancel. Replaces the "edit in Django admin"
fallback that shipped with the OAuth-only page.
"""

import datetime as _dt
import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required

from .models import AvailabilityWindow, DAY_CHOICES, ScheduledCall

logger = logging.getLogger(__name__)


# Default windows used by the "Seed defaults" button. Matches the spec:
# Mon–Fri 4–8pm ET, Sat 9am–8pm ET, Sun closed.
DEFAULT_WINDOWS = [
    (0, _dt.time(16, 0), _dt.time(20, 0)),  # Mon
    (1, _dt.time(16, 0), _dt.time(20, 0)),  # Tue
    (2, _dt.time(16, 0), _dt.time(20, 0)),  # Wed
    (3, _dt.time(16, 0), _dt.time(20, 0)),  # Thu
    (4, _dt.time(16, 0), _dt.time(20, 0)),  # Fri
    (5, _dt.time(9, 0), _dt.time(20, 0)),   # Sat
]


def _parse_time(value):
    """Accept '16:00' or '4:00 PM' — return _dt.time or None."""
    if not value:
        return None
    value = value.strip()
    for fmt in ('%H:%M', '%I:%M %p', '%I:%M%p', '%H:%M:%S'):
        try:
            return _dt.datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


@admin_required
def availability_list(request):
    """One page — list + add form. Edit/delete are inline POSTs."""
    windows = list(AvailabilityWindow.objects.order_by(
        'day_of_week', 'start_time'))

    # Group by day for the table — every day shown so admin can see gaps
    by_day = []
    for dow, day_label in DAY_CHOICES:
        day_windows = [w for w in windows if w.day_of_week == dow]
        by_day.append({
            'day_of_week': dow,
            'day_label': day_label,
            'windows': day_windows,
        })

    return render(request, 'scheduler/availability_list.html', {
        'active': 'schedule',
        'by_day': by_day,
        'day_choices': DAY_CHOICES,
        'total_windows': len(windows),
    })


@admin_required
@require_POST
def availability_add(request):
    dow_raw = request.POST.get('day_of_week', '')
    start_raw = request.POST.get('start_time', '')
    end_raw = request.POST.get('end_time', '')
    tz_raw = (request.POST.get('timezone') or 'America/New_York').strip()

    try:
        dow = int(dow_raw)
        if dow not in {c[0] for c in DAY_CHOICES}:
            raise ValueError('bad day')
    except ValueError:
        messages.error(request, 'Pick a day of the week.')
        return redirect('admin_dashboard:schedule_availability')

    start_t = _parse_time(start_raw)
    end_t = _parse_time(end_raw)
    if not start_t or not end_t:
        messages.error(
            request, 'Start and end times required (HH:MM, 24-hour).')
        return redirect('admin_dashboard:schedule_availability')
    if start_t >= end_t:
        messages.error(request, 'End time must be after start time.')
        return redirect('admin_dashboard:schedule_availability')

    AvailabilityWindow.objects.create(
        day_of_week=dow,
        start_time=start_t,
        end_time=end_t,
        timezone=tz_raw,
        active=True,
    )
    messages.success(
        request,
        f'Added: {dict(DAY_CHOICES)[dow]} '
        f'{start_t:%H:%M}–{end_t:%H:%M} {tz_raw}.')
    return redirect('admin_dashboard:schedule_availability')


@admin_required
@require_POST
def availability_edit(request, window_id):
    w = get_object_or_404(AvailabilityWindow, pk=window_id)

    start_t = _parse_time(request.POST.get('start_time', ''))
    end_t = _parse_time(request.POST.get('end_time', ''))
    tz_raw = (request.POST.get('timezone') or w.timezone).strip()

    if not start_t or not end_t or start_t >= end_t:
        messages.error(
            request,
            'Bad times — end must be after start, both in HH:MM.')
        return redirect('admin_dashboard:schedule_availability')

    w.start_time = start_t
    w.end_time = end_t
    w.timezone = tz_raw
    w.save(update_fields=['start_time', 'end_time', 'timezone'])
    messages.success(request, f'Updated {w}.')
    return redirect('admin_dashboard:schedule_availability')


@admin_required
@require_POST
def availability_toggle(request, window_id):
    w = get_object_or_404(AvailabilityWindow, pk=window_id)
    w.active = not w.active
    w.save(update_fields=['active'])
    messages.info(
        request,
        f'{w.get_day_of_week_display()} '
        f'{w.start_time:%H:%M}–{w.end_time:%H:%M} '
        f'{"ENABLED" if w.active else "DISABLED"}.')
    return redirect('admin_dashboard:schedule_availability')


@admin_required
@require_POST
def availability_delete(request, window_id):
    w = get_object_or_404(AvailabilityWindow, pk=window_id)
    label = str(w)
    w.delete()
    messages.info(request, f'Deleted {label}.')
    return redirect('admin_dashboard:schedule_availability')


@admin_required
@require_POST
def availability_seed_defaults(request):
    """One-click: create the spec defaults if missing."""
    created = 0
    for dow, start_t, end_t in DEFAULT_WINDOWS:
        _, was_created = AvailabilityWindow.objects.get_or_create(
            day_of_week=dow,
            start_time=start_t,
            end_time=end_t,
            timezone='America/New_York',
            defaults={'active': True},
        )
        if was_created:
            created += 1
    if created:
        messages.success(
            request, f'Seeded {created} default availability window(s).')
    else:
        messages.info(
            request, 'Defaults already in place — nothing added.')
    return redirect('admin_dashboard:schedule_availability')


# ─── Scheduled calls ───────────────────────────────────────────────────


@admin_required
def calls_list(request):
    """All ScheduledCalls — newest first, with status filter."""
    status_filter = (request.GET.get('status') or '').strip()
    qs = ScheduledCall.objects.all().select_related('lead').order_by(
        '-starts_at')
    if status_filter and status_filter in {
            'held', 'confirmed', 'cancelled', 'completed'}:
        qs = qs.filter(status=status_filter)

    upcoming = qs.filter(starts_at__gte=timezone.now())[:50]
    past = qs.filter(starts_at__lt=timezone.now())[:50]

    return render(request, 'scheduler/calls_list.html', {
        'active': 'schedule',
        'upcoming': upcoming,
        'past': past,
        'status_filter': status_filter,
        'status_choices': [
            ('', 'All'),
            ('held', 'Held'),
            ('confirmed', 'Confirmed'),
            ('cancelled', 'Cancelled'),
            ('completed', 'Completed'),
        ],
    })


@admin_required
@require_POST
def call_cancel(request, call_id):
    call = get_object_or_404(ScheduledCall, pk=call_id)
    if call.status == 'cancelled':
        messages.info(request, 'Call already cancelled.')
        return redirect('admin_dashboard:schedule_calls')

    # Tear down the Google Calendar event if we pushed one
    if call.google_event_id:
        try:
            from .google_calendar import cancel_event_for_call
            cancel_event_for_call(call)
        except Exception:
            logger.exception('cancel_event_for_call failed')

    call.status = 'cancelled'
    call.save(update_fields=['status'])
    messages.success(
        request,
        f'Cancelled {call.customer_name or "(anon)"} '
        f'@ {call.starts_at:%b %d %H:%M}.')
    return redirect('admin_dashboard:schedule_calls')


@admin_required
@require_POST
def call_mark_completed(request, call_id):
    call = get_object_or_404(ScheduledCall, pk=call_id)
    call.status = 'completed'
    call.save(update_fields=['status'])
    messages.success(request, 'Marked completed.')
    return redirect('admin_dashboard:schedule_calls')
