"""
Scheduler email senders — confirmation to the customer + admin
notification when a Schedule-a-Call form is submitted.

Both use ``send_mail`` (which routes through SendGrid in production —
see CLAUDE.md → Email/SendGrid). Failures use logger.exception so the
operator sees them in the supervisor log AND surfaces via the new
SystemAlert widget on the admin dashboard.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

logger = logging.getLogger(__name__)


def _alert(severity, source, message, detail=''):
    """Best-effort SystemAlert write so failures appear on the dashboard."""
    try:
        from core.system_alerts import record_alert
        record_alert(severity=severity, source=source,
                     message=message, detail=detail)
    except Exception:
        pass


def send_schedule_confirmation_to_customer(call):
    """One-email to the customer the moment they confirm a slot."""
    if not call.customer_email:
        return
    when_str = call.starts_at.strftime('%A, %B %-d at %-I:%M %p %Z') \
        if call.starts_at else ''
    body = render_to_string(
        'scheduler/emails/customer_confirmation.txt', {
            'call': call,
            'when_str': when_str,
        }
    )
    try:
        send_mail(
            subject='Your call with Aspired Websites is confirmed',
            message=body,
            from_email=getattr(
                settings, 'DEFAULT_FROM_EMAIL',
                'zachery@aspiredwebsites.com'),
            recipient_list=[call.customer_email],
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
    """Heads-up email to the operator (Zachery) about the new booking."""
    admin_email = getattr(
        settings, 'LEAD_NOTIFICATION_EMAIL',
        getattr(settings, 'DEFAULT_FROM_EMAIL',
                'zachery@aspiredwebsites.com'))
    when_str = call.starts_at.strftime('%A, %B %-d at %-I:%M %p %Z') \
        if call.starts_at else ''
    lead_url = ''
    if call.lead_id:
        base = getattr(
            settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
        lead_url = f'{base}/admin-dashboard/leads/{call.lead_id}/'
    body = render_to_string(
        'scheduler/emails/admin_notification.txt', {
            'call': call,
            'when_str': when_str,
            'lead_url': lead_url,
        }
    )
    try:
        send_mail(
            subject=(f'New kickoff call booked — {call.customer_name or "?"} '
                     f'@ {when_str}'),
            message=body,
            from_email=getattr(
                settings, 'DEFAULT_FROM_EMAIL',
                'zachery@aspiredwebsites.com'),
            recipient_list=[admin_email],
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
