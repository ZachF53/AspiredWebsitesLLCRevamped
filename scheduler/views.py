"""Public-facing Schedule-a-Call page + slot API."""

import datetime as _dt
import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .availability import enumerate_slots
from .models import ScheduledCall


def schedule_page(request):
    """The /design/schedule/ page — calendar widget + contact form."""
    from billing.pricing_models import ServiceTier

    addons = (ServiceTier.objects
              .filter(category__in=('maintenance', 'social_media'),
                      is_active=True)
              .order_by('category', 'price'))
    return render(request, 'scheduler/schedule.html', {
        'addons': addons,
    })


def slots_api(request):
    """JSON list of available slots over the next N days (default 21)."""
    days = int(request.GET.get('days') or 21)
    days = max(1, min(days, 60))
    today = timezone.localdate()
    end_date = today + _dt.timedelta(days=days)
    slots = list(enumerate_slots(today, end_date))
    return JsonResponse({
        'slots': [
            {
                'start': s.isoformat(),
                'end': e.isoformat(),
                'label': s.strftime('%a %b %-d %I:%M %p'),
            }
            for s, e in slots
        ],
    })


@csrf_exempt
@require_POST
def hold_slot(request):
    """POST a slot to hold it for 15 minutes (form-completion window)."""
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return HttpResponse('bad json', status=400)
    starts_iso = payload.get('starts_at') or ''
    if not starts_iso:
        return HttpResponse('starts_at required', status=400)
    try:
        starts_at = _dt.datetime.fromisoformat(starts_iso.replace('Z', '+00:00'))
    except ValueError:
        return HttpResponse('bad starts_at', status=400)

    # Reject if already held / confirmed by someone else
    if ScheduledCall.objects.filter(
            starts_at=starts_at,
        ).exclude(status='cancelled').exists():
        return JsonResponse({'error': 'slot already taken'}, status=409)

    ends_at = starts_at + _dt.timedelta(minutes=30)
    expires_at = timezone.now() + _dt.timedelta(minutes=15)
    call = ScheduledCall.objects.create(
        starts_at=starts_at, ends_at=ends_at, status='held',
        expires_at=expires_at,
    )
    return JsonResponse({'ok': True, 'call_id': str(call.id)})


@csrf_exempt
@require_POST
def confirm_slot(request):
    """Confirm a previously-held slot with the customer's contact form."""
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return HttpResponse('bad json', status=400)

    call_id = payload.get('call_id') or ''
    if not call_id:
        return HttpResponse('call_id required', status=400)
    try:
        call = ScheduledCall.objects.get(id=call_id, status='held')
    except ScheduledCall.DoesNotExist:
        return JsonResponse({'error': 'slot no longer held'}, status=404)
    if call.expires_at and call.expires_at < timezone.now():
        return JsonResponse({'error': 'hold expired'}, status=410)

    name = (payload.get('name') or '').strip()
    email = (payload.get('email') or '').strip().lower()
    phone = (payload.get('phone') or '').strip()
    business = (payload.get('business') or '').strip()
    website = (payload.get('website') or '').strip()
    build_type = (payload.get('build_type') or '').strip()
    inquiry = (payload.get('inquiry') or '').strip()
    addons = payload.get('addons') or []
    if not (name and email and business):
        return JsonResponse({
            'error': 'name, email, business required'}, status=400)

    # Create a Lead with the opt-in flags
    try:
        from outreach.models import Lead
        lead = Lead.objects.create(
            firm_name=business,
            attorney_name=name,
            email=email,
            phone=phone,
            website=website,
            source='schedule_call' if 'schedule_call' in dict(
                getattr(Lead, 'SOURCE_CHOICES', [])) else 'contact_form',
            inquiry_text=inquiry,
            tags=f'build_type:{build_type}',
            status='new',
        )
        # Save opt-ins if the Lead model has the field
        if hasattr(lead, 'opted_in_addons'):
            lead.opted_in_addons = addons
            lead.opted_in_addons_at = timezone.now()
            lead.save(update_fields=['opted_in_addons', 'opted_in_addons_at'])
        call.lead = lead
    except Exception:
        pass

    call.customer_name = name
    call.customer_email = email
    call.notes = inquiry
    call.status = 'confirmed'
    call.save(update_fields=[
        'lead', 'customer_name', 'customer_email', 'notes', 'status'])

    # Try to push to Google Calendar — fall back silently if not connected
    try:
        from .google_calendar import push_event_for_call
        push_event_for_call(call)
    except Exception:
        pass

    # Fire confirmation emails — customer + admin. Each has its own
    # try/except inside the sender, so a SendGrid failure on one
    # doesn't block the other (or the response).
    from .emails import (
        send_schedule_confirmation_to_customer,
        send_schedule_notification_to_admin,
    )
    send_schedule_confirmation_to_customer(call)
    send_schedule_notification_to_admin(call)

    return JsonResponse({'ok': True})
