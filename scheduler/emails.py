"""
Scheduler email senders — branded HTML confirmation to the customer
plus a branded admin notification when a /design/schedule/ form
completes.

Both use clients.emails.send_branded so we get the same dark-themed
card layout as the other transactional emails, and SendGrid click +
open tracking is disabled site-wide by AspiredEmailBackend (see
core/email_backend.py).

Failures log via logger.exception and write a SystemAlert so they
surface on the admin dashboard banner without anyone having to SSH
into the box.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _alert(severity, source, message, detail=''):
    """Best-effort SystemAlert write so failures appear on the dashboard."""
    try:
        from core.system_alerts import record_alert
        record_alert(severity=severity, source=source,
                     message=message, detail=detail)
    except Exception:
        pass


def _format_when(dt, tz_name='America/New_York'):
    """ISO datetimes are UTC server-side; render as 'Thursday, June 11
    at 4:00 PM EDT' in the AvailabilityWindow timezone so the body
    matches what the operator configured."""
    if not dt:
        return ''
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    tz_abbr = local.strftime('%Z') or 'ET'
    # Cross-platform: avoid %-d / %-I (Windows strftime doesn't grok them).
    return '{}, {} {} at {}:{:02d} {} {}'.format(
        local.strftime('%A'),
        local.strftime('%B'),
        local.day,
        local.hour % 12 or 12,
        local.minute,
        'AM' if local.hour < 12 else 'PM',
        tz_abbr,
    )


def _first_name(call):
    """Best-effort first name from the call's customer_name."""
    raw = (call.customer_name or '').strip()
    if not raw:
        return 'there'
    return raw.split(' ')[0]


def _lead_attrs(call):
    """Pull phone / business / website / build_type from the linked Lead
    if there is one — needed to populate the admin notification body."""
    out = {'phone': '', 'business': '', 'website': '', 'build_type': ''}
    if not call.lead_id:
        return out
    try:
        lead = call.lead
    except Exception:
        return out
    if not lead:
        return out
    out['phone'] = (lead.phone or '').strip()
    out['business'] = (lead.firm_name or '').strip()
    out['website'] = (lead.website or '').strip()
    # build_type is stored in Lead.tags as "build_type:essential" etc.
    tags = (lead.tags or '')
    for chunk in [t.strip() for t in tags.split(',') if t.strip()]:
        if chunk.startswith('build_type:'):
            out['build_type'] = chunk.split(':', 1)[1]
            break
    return out


_BUILD_TYPE_LABELS = {
    'essential': 'Essential Build',
    'premium':   'Premium Build',
    'not_sure':  'Not sure yet',
}


def send_schedule_confirmation_to_customer(call):
    """Branded confirmation to the customer the moment they confirm a slot."""
    if not call.customer_email:
        return

    from clients.emails import send_branded

    when_str = _format_when(call.starts_at)
    lead_attrs = _lead_attrs(call)
    context = {
        'first_name': _first_name(call),
        'when_str': when_str,
        'phone': lead_attrs['phone'],
        'inquiry': call.notes or '',
        'preheader': f'See you {when_str}.' if when_str else '',
    }
    text_body = (
        f"You're booked, {context['first_name']}.\n\n"
        f"When: {when_str}\n"
        f"30-minute strategy call · phone\n\n"
        f"I'll call {context['phone'] or 'the number on file'} at that "
        f"time. If anything changes, just reply to this email and "
        f"we'll re-pick.\n\n"
        f"Before we talk, a few things help me prep:\n"
        f"  - Two or three sites you like the look of\n"
        f"  - Anything you definitely don't want\n"
        f"  - Your current domain, if you have one\n\n"
        f"Talk soon,\n"
        f"Zachery\n"
    )

    try:
        send_branded(
            subject='Your call with Aspired Websites is confirmed',
            template='schedule_confirmation',
            context=context,
            recipient_list=[call.customer_email],
            text_body=text_body,
            from_email=getattr(
                settings, 'DEFAULT_FROM_EMAIL',
                'zacherylong@aspiredwebsites.com'),
            fail_silently=False,
        )
        logger.info(
            'schedule confirmation email sent to %s for call %s',
            call.customer_email, call.pk)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'schedule confirmation email FAILED for call %s', call.pk)
        _alert(
            severity='error',
            source='scheduler.confirmation_email',
            message=f'Confirmation email failed for {call.customer_email}',
            detail=str(exc)[:1000],
        )


def send_schedule_notification_to_admin(call):
    """Branded heads-up email to the operator about the new booking."""
    from clients.emails import send_branded

    admin_email = getattr(
        settings, 'LEAD_NOTIFICATION_EMAIL',
        getattr(settings, 'DEFAULT_FROM_EMAIL',
                'zacherylong@aspiredwebsites.com'))
    when_str = _format_when(call.starts_at)
    lead_attrs = _lead_attrs(call)

    lead_url = ''
    addons = []
    if call.lead_id:
        base = getattr(
            settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
        lead_url = f'{base}/admin-dashboard/leads/{call.lead_id}/'
        try:
            addons = list(call.lead.opted_in_addons or [])
        except Exception:
            addons = []

    build_type = lead_attrs['build_type']
    context = {
        'customer_name': call.customer_name or '',
        'customer_email': call.customer_email or '',
        'phone': lead_attrs['phone'],
        'business': lead_attrs['business'],
        'website': lead_attrs['website'],
        'build_type': build_type,
        'build_type_display': _BUILD_TYPE_LABELS.get(build_type, build_type),
        'addons': addons,
        'inquiry': call.notes or '',
        'when_str': when_str,
        'lead_url': lead_url,
        'preheader': f'New strategy call · {when_str}',
    }
    text_body = (
        f"New strategy call booked.\n\n"
        f"When: {when_str}\n"
        f"Name: {call.customer_name or '—'}\n"
        f"Email: {call.customer_email or '—'}\n"
        f"Phone: {lead_attrs['phone'] or '—'}\n"
        f"Business: {lead_attrs['business'] or '—'}\n"
        f"Build type: "
        f"{_BUILD_TYPE_LABELS.get(build_type, build_type) or '—'}\n"
        f"Opt-in add-ons: {', '.join(addons) if addons else '—'}\n\n"
        f"What they want to build:\n{call.notes or '(none provided)'}\n\n"
        f"Lead: {lead_url or '(no lead linked)'}\n"
    )

    subject = (f'New strategy call booked — {call.customer_name or "?"} '
               f'@ {when_str}')

    try:
        send_branded(
            subject=subject,
            template='schedule_admin_notification',
            context=context,
            recipient_list=[admin_email],
            text_body=text_body,
            from_email=getattr(
                settings, 'DEFAULT_FROM_EMAIL',
                'zacherylong@aspiredwebsites.com'),
            fail_silently=False,
        )
        logger.info(
            'schedule admin notification sent for call %s', call.pk)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'schedule admin notification FAILED for call %s', call.pk)
        _alert(
            severity='error',
            source='scheduler.admin_notification',
            message=f'Admin notification email failed for call {call.pk}',
            detail=str(exc)[:1000],
        )
