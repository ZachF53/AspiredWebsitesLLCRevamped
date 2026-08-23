"""Celery tasks for the public site."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def send_audit_followups_task():
    """Daily — send follow-up 1 and 2 to people who ran an audit.

    Warm, not cold: these people typed their own URL in and handed over
    an email to get the results. They asked.

    Runs once a day rather than hourly because the schedule is measured
    in days and a follow-up arriving at 09:00 reads better than one
    arriving at 14:37. Every send is guarded by its own timestamp field,
    so a double-run inside the same day sends nothing twice.
    """
    from datetime import timedelta

    from django.utils import timezone

    from public.audit_sequence import (
        FOLLOWUP_1_DAYS, FOLLOWUP_2_DAYS, FOLLOWUP_2_MIN_GAP_DAYS,
        send_followup,
    )
    from public.models import AuditLead

    now = timezone.now()
    sent_1 = sent_2 = skipped = failed = 0

    # Follow-up 1: report sent at least FOLLOWUP_1_DAYS ago, none sent yet.
    due_1 = AuditLead.objects.filter(
        report_sent_at__isnull=False,
        report_sent_at__lte=now - timedelta(days=FOLLOWUP_1_DAYS),
        followup_1_sent_at__isnull=True,
        unsubscribed=False,
    ).exclude(email='')

    # Follow-up 2: both windows must be satisfied. The gap after
    # follow-up 1 is what stops a record whose report sat unsent for ten
    # days from receiving both follow-ups in the same batch, minutes
    # apart -- which is what happened before this second condition
    # existed, and reads exactly like the broken system it is.
    due_2 = AuditLead.objects.filter(
        report_sent_at__isnull=False,
        report_sent_at__lte=now - timedelta(days=FOLLOWUP_2_DAYS),
        followup_1_sent_at__isnull=False,
        followup_1_sent_at__lte=now - timedelta(
            days=FOLLOWUP_2_MIN_GAP_DAYS),
        followup_2_sent_at__isnull=True,
        unsubscribed=False,
    ).exclude(email='')

    for lead in due_1:
        try:
            if send_followup(lead, 1):
                sent_1 += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the batch
            logger.exception('audit follow-up 1 failed for %s', lead.pk)
            failed += 1

    for lead in due_2:
        try:
            if send_followup(lead, 2):
                sent_2 += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001
            logger.exception('audit follow-up 2 failed for %s', lead.pk)
            failed += 1

    result = (f'followup_1={sent_1} followup_2={sent_2} '
              f'skipped={skipped} failed={failed}')
    logger.info('send_audit_followups_task: %s', result)
    return result
