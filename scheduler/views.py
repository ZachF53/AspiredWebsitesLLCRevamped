"""Public-facing Schedule-a-Call page + slot API."""

import datetime as _dt
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .availability import BLOCK_MINUTES, MIN_LEAD_MINUTES, enumerate_slots
from .models import ScheduledCall

logger = logging.getLogger(__name__)

# Schedule-page build-type form value → ClientProfile/Website package code.
_BUILD_TYPE_TO_PACKAGE = {
    'essential': 'essential_build',
    'premium': 'premium_build',
}


def _attribute_booking_to_variant(email):
    """Credit a booked call to the template variant that produced the
    outreach the prospect actually received.

    Walks: address → the Lead(s) with that address → their most recent
    SENT cold email → its ``template_variant`` → bump ``bookings``.

    "Most recent sent" is the honest attribution at this volume. A real
    multi-touch model would weight every touch in the sequence, but with
    a four-step sequence and single-digit bookings that would be
    precision theatre — last-touch is defensible and legible.

    Returns the variant id credited, or None when the booking did not
    come from cold outreach at all (inbound contact form, referral,
    someone who found the site directly). That None is the common case
    and is not an error.
    """
    if not email:
        return None

    from outreach.models import EmailSent
    from outreach.variant_rotation import record_booking

    last_sent = (
        EmailSent.objects
        .filter(lead__email__iexact=email, status='sent', kind='cold')
        .exclude(template_variant__isnull=True)
        .order_by('-sent_at')
        .first()
    )
    if last_sent is None:
        return None

    record_booking(last_sent.template_variant_id)
    logger.info(
        'booking attributed to template variant %s (EmailSent %s, step %s)',
        last_sent.template_variant_id, last_sent.pk, last_sent.sequence_step)
    return last_sent.template_variant_id


def _provision_webdev_inquiry(*, email, business, contact_name, phone,
                              website, build_package, addons):
    """For a Website Development booking, create (or reuse) an inactive
    User + Account + Website so the contract, invoice, and account setup all
    tie to a real account from the moment they book. Returns the Website or
    None. Never raises — a provisioning hiccup must not fail the booking.

    The maintenance/social opt-ins are recorded on the Website to drive the
    "go Live → start billing (10% off first month)" flow later.
    """
    try:
        from django.contrib.auth import get_user_model

        from billing.pricing_models import ServiceTier
        from clients.account_models import Account, Website, _slugify_unique

        User = get_user_model()
        if not email:
            return None

        # Split opt-ins into the maintenance + social picks.
        maint_tier = social_tier = ''
        if addons:
            cats = dict(
                ServiceTier.objects.filter(slug__in=addons)
                .values_list('slug', 'category'))
            for slug in addons:
                cat = cats.get(slug)
                if cat == 'maintenance' and not maint_tier:
                    maint_tier = slug
                elif cat == 'social_media' and not social_tier:
                    social_tier = slug

        user, _created = User.objects.get_or_create(
            username=email, defaults={'email': email, 'is_active': False})
        if not user.email:
            user.email = email
            user.save(update_fields=['email'])

        # The Account is created directly. This used to create a
        # ClientProfile and rely on a signal to materialise the Account
        # behind it, then repair the result with ensure_account when the
        # signal had failed -- two writes and a repair pass to produce the
        # row we can simply create.
        account, _ = Account.objects.get_or_create(
            user=user,
            defaults={
                'name': business or email,
                'contact_name': contact_name or '',
                'phone': phone or '',
            },
        )
        changed = []
        if business and not account.name:
            account.name = business
            changed.append('name')
        if contact_name and not account.contact_name:
            account.contact_name = contact_name
            changed.append('contact_name')
        if phone and not account.phone:
            account.phone = phone
            changed.append('phone')
        if changed:
            account.save(update_fields=changed + ['updated_at'])

        web = account.websites.order_by('created_at').first()
        if web is None:
            web = Website.objects.create(
                account=account, name=business or 'Website',
                slug=_slugify_unique(business or 'website', Website))

        web.lifecycle_status = 'inquiry'
        web.opted_in_maintenance_tier = maint_tier
        web.opted_in_social_tier = social_tier
        if build_package:
            web.package = build_package
        if business:
            web.name = business
        if website and not web.url:
            web.url = website
        web.save(update_fields=[
            'lifecycle_status', 'opted_in_maintenance_tier',
            'opted_in_social_tier', 'package', 'name', 'url', 'updated_at'])
        return web
    except Exception:
        logger.exception('scheduler: web-dev inquiry provisioning failed')
        return None


# Per-service copy + form configuration. Each service uses the SAME
# calendar widget + Google Calendar push pipeline, just a different
# H1, inquiry prompt, and per-service custom field. Add a new service
# here + a URL route in scheduler/urls.py and it just works.
SERVICE_CONFIG = {
    'web_design': {
        'eyebrow': 'Book a Strategy Call',
        'h1_pre': 'Let’s talk about your ',
        'h1_accent': 'build',
        'h1_post': '.',
        'lead': '30-minute Strategy Call — pick a time that works for you.',
        'show_build_type': True,
        'show_addons': False,
        'inquiry_label': 'What are you trying to build?',
        'website_label': 'Existing website (if any)',
        'meta_title': 'Schedule a Call — Web Design',
    },
    'social_media': {
        'eyebrow': 'Book a Strategy Call',
        'h1_pre': 'Let’s talk about your ',
        'h1_accent': 'social presence',
        'h1_post': '.',
        'lead': '30-minute Strategy Call — pick a time that works for you.',
        'show_build_type': False,
        'show_addons': False,
        'inquiry_label': 'Tell us about your current presence and what you want it to do for the business.',
        'website_label': 'Website (if any)',
        'meta_title': 'Schedule a Call — Social Media Strategy',
    },
    'seo': {
        'eyebrow': 'Book a Strategy Call',
        'h1_pre': 'Let’s talk about your ',
        'h1_accent': 'rankings',
        'h1_post': '.',
        'lead': '30-minute Strategy Call — pick a time that works for you.',
        'show_build_type': False,
        'show_addons': False,
        'inquiry_label': 'What are you trying to rank for, and in which cities?',
        'website_label': 'Current website',
        'meta_title': 'Schedule a Call — SEO Strategy',
    },
}


# ensure_csrf_cookie so the CSRF cookie exists for schedule_call.js to
# read. The booking POSTs are pure fetch() with no Django form on the
# page, so nothing else would set it — which is why hold/confirm were
# @csrf_exempt: the JS sent an EMPTY X-CSRFToken and the only way the
# endpoints worked was by not checking. Setting the cookie here is what
# makes real CSRF protection possible on those two endpoints.
@ensure_csrf_cookie
@ratelimit(key='ip', rate='30/h', method='POST', block=True)
def schedule_page(request, service='web_design'):
    """Universal schedule page — same calendar widget, service-specific
    copy + form fields. Three URLs map here:
        /design/schedule/  → service='web_design'  (default, legacy)
        /social/schedule/  → service='social_media'
        /seo/schedule/     → service='seo'
    """
    from billing.pricing_models import ServiceTier

    config = SERVICE_CONFIG.get(service) or SERVICE_CONFIG['web_design']

    # Addons — every plan (maintenance + social) gets surfaced as a
    # cross-sell on every service page (even social/seo) since the
    # 10%-off-first-month promise still applies.
    addons = (ServiceTier.objects
              .filter(category__in=('maintenance', 'social_media'),
                      is_active=True)
              .order_by('category', 'price')) if config['show_addons'] else []

    # Build-type options — only relevant for the web-design schedule
    # page. Pulled from the pricing DB so the dropdown labels stay in
    # sync if prices ever change.
    build_tiers = []
    if config['show_build_type']:
        for t in (ServiceTier.objects
                  .filter(category='website_build', is_active=True)
                  .order_by('price')):
            if 'essential' in t.slug:
                form_value = 'essential'
            elif 'premium' in t.slug:
                form_value = 'premium'
            else:
                form_value = t.slug
            build_tiers.append({
                'value': form_value,
                'name': t.name,
                'price': t.get_price_display(),
            })

    # A POST here is the no-JavaScript fallback: the booking flow itself
    # is fetch()-driven, so reaching this branch means the calendar never
    # ran and no slot was ever selected. There is nothing to book.
    #
    # It is still answered deliberately rather than ignored. The form now
    # declares method="post" precisely so this submit cannot become a GET
    # that puts the visitor's name, email, phone and project description
    # into the URL. Nothing is stored and no mail is sent from here — a
    # booking without a chosen slot is not a booking, and inventing a
    # lead-capture side effect would be a different product decision.
    # The visitor is told plainly and pointed at a path that works.
    fallback_notice = ''
    if request.method == 'POST':
        fallback_notice = (
            'We could not load the calendar in your browser, so no time '
            'was reserved. Nothing you typed was saved. Please send us a '
            'message and we will arrange a time by email.')

    return render(request, 'scheduler/schedule.html', {
        'service': service,
        'service_config': config,
        'addons': addons,
        'build_tiers': build_tiers,
        'fallback_notice': fallback_notice,
    })


def slots_api(request):
    """JSON list of available slots over the next N days (default 60).

    60 days is enough for the calendar widget to render two months
    forward (current + next) without paging back to the server.
    """
    days = int(request.GET.get('days') or 60)
    days = max(1, min(days, 90))
    today = timezone.localdate()
    end_date = today + _dt.timedelta(days=days)
    slots = list(enumerate_slots(today, end_date))
    # `start` / `end` are ISO 8601 with offset. The frontend formats
    # them in the visitor's local timezone — server-side strftime
    # would lock everyone into UTC, which previously caused 4 PM ET
    # windows to render as 8 PM on the booking page.
    return JsonResponse({
        'slots': [
            {'start': s.isoformat(), 'end': e.isoformat()}
            for s, e in slots
        ],
    })


# CSRF protection ON. This was @csrf_exempt, which it never needed:
# unlike the Stripe/SendGrid/sync webhooks (signature-verified) and the
# cross-origin tracker endpoints, this is same-origin — it is only ever
# called by schedule_call.js on our own booking page, and that already
# sends X-CSRFToken. The exemption was attack surface with nothing
# behind it: a third-party page could make a visitor's browser hold
# slots, and 15/h per IP only bounds the abuse rather than stopping it.
@require_POST
@ratelimit(key='ip', rate='15/h', method='POST', block=True)
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

    # Lead-time floor — same MIN_LEAD_MINUTES we use in the enumerator,
    # re-checked here so a stale tab from before lead time elapsed
    # can't sneak a booking in. Use a 60s grace to avoid clock drift
    # blocking the very moment a slot becomes legal.
    earliest = timezone.now() + _dt.timedelta(
        minutes=MIN_LEAD_MINUTES, seconds=-60)
    if starts_at < earliest:
        return JsonResponse(
            {'error': 'too close to start time'}, status=409)

    # Each booking blocks BLOCK_MINUTES (2 hours) on the calendar —
    # so we reject any new slot whose [start, start+30min] overlaps
    # an existing non-cancelled block, not just an exact start match.
    new_end = starts_at + _dt.timedelta(minutes=30)
    if ScheduledCall.objects.filter(
            starts_at__lt=new_end,
            ends_at__gt=starts_at,
        ).exclude(status='cancelled').exists():
        return JsonResponse({'error': 'slot already taken'}, status=409)

    ends_at = starts_at + _dt.timedelta(minutes=BLOCK_MINUTES)
    expires_at = timezone.now() + _dt.timedelta(minutes=15)
    call = ScheduledCall.objects.create(
        starts_at=starts_at, ends_at=ends_at, status='held',
        expires_at=expires_at,
    )
    return JsonResponse({'ok': True, 'call_id': str(call.id)})


# Same as hold_slot — same-origin, and the caller already sends the token.
@require_POST
@ratelimit(key='ip', rate='15/h', method='POST', block=True)
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
    service = (payload.get('service') or 'web_design').strip()
    if not (name and email and business):
        return JsonResponse({
            'error': 'name, email, business required'}, status=400)

    # Build tag string — service prefix lets the operator triage the
    # admin pipeline by what the lead is interested in. Build-type
    # only appended for web-design leads where it's meaningful.
    tag_parts = [f'service:{service}']
    if build_type:
        tag_parts.append(f'build_type:{build_type}')
    tags_str = ','.join(tag_parts)

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
            tags=tags_str,
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

    # Attribute the booking back to the outreach angle that earned it.
    # A booked call is the only outcome that actually matters — opens and
    # replies are proxies for it — so this is the counter the variant
    # rotation ultimately wants to optimise on.
    #
    # Matching is by email rather than by `call.lead`: the Lead created
    # above is brand new, whereas the person booking may have been in the
    # cold sequence for weeks under a lead row we already had. Best-effort
    # throughout — a booking must never fail over attribution bookkeeping.
    try:
        _attribute_booking_to_variant(email)
    except Exception:  # noqa: BLE001
        logger.exception('booking attribution failed for %s', email)

    # Website Development bookings (a build tier was chosen) provision an
    # inactive account + website up front, so the contract / invoice / setup
    # all tie to that user by email. Other services stay leads only.
    if service == 'web_design' and build_type:
        _provision_webdev_inquiry(
            email=email,
            business=business,
            contact_name=name,
            phone=phone,
            website=website,
            build_package=_BUILD_TYPE_TO_PACKAGE.get(build_type, ''),
            addons=addons,
        )

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
