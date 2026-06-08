"""
Celery tasks for the onboarding/SetupTodo widget.
"""

import datetime
import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def send_setup_todo_reminders():
    """
    Daily — send day-3 / day-7 / day-14 reminder emails for any
    SetupTodo that is still pending past that threshold and hasn't
    received that reminder yet. One email per (user, day-bucket).
    """
    from .todo_models import SetupTodo

    now = timezone.now()
    buckets = [
        (3,  'reminder_3_sent'),
        (7,  'reminder_7_sent'),
        (14, 'reminder_14_sent'),
    ]
    total_sent = 0
    for days, flag_field in buckets:
        cutoff = now - datetime.timedelta(days=days)
        # All pending todos whose user hasn't received this bucket's
        # reminder yet and whose creation crossed the threshold.
        todos = (SetupTodo.objects
                 .filter(status='pending',
                         created_at__lte=cutoff,
                         **{flag_field: False})
                 .select_related('user'))
        # Group by user so each user gets ONE email with all pending items
        by_user = {}
        for t in todos:
            by_user.setdefault(t.user_id, {'user': t.user, 'items': []})
            by_user[t.user_id]['items'].append(t)
        for bundle in by_user.values():
            try:
                _send_reminder_email(bundle['user'], bundle['items'], days)
                # Mark every item in this bundle as having sent THIS reminder
                ids = [i.pk for i in bundle['items']]
                SetupTodo.objects.filter(pk__in=ids).update(**{flag_field: True})
                total_sent += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    'todo reminder day-%s send failed for user %s',
                    days, bundle['user'].pk)
    return f'sent {total_sent} reminder email(s)'


def _send_reminder_email(user, items, day_bucket):
    """One reminder email per user listing every open To-Do item."""
    subject_map = {
        3:  'Reminder — a few things still pending in your portal',
        7:  'Still pending: items in your portal we need from you',
        14: 'Last reminder — please complete these to-do items',
    }
    subject = subject_map.get(day_bucket, 'Reminder — items pending in your portal')
    lines = ['Hi,', '',
             'A few items from your onboarding are still open on your '
             'portal To-Do list:', '']
    for t in items:
        lines.append(f'  • {t.title}')
    lines.extend([
        '',
        'Log into your portal and click "To-Do List" in the sidebar to '
        'complete them: https://aspiredwebsites.com/portal/',
        '',
        'Thanks!',
        '— Aspired Websites',
    ])
    send_mail(
        subject=subject,
        message='\n'.join(lines),
        from_email=getattr(
            settings, 'DEFAULT_FROM_EMAIL',
            'zachery@aspiredwebsites.com'),
        recipient_list=[user.email],
        fail_silently=False,
    )
