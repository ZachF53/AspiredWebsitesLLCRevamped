"""
Outbound sync signals (Aspired → Moonieful).

When a Website's stage or maintenance_active flag changes locally, queue a
SyncJob so run_sync can notify Moonieful. Changes that originated from an
*inbound* sync carry `instance._from_sync = True` and are skipped,
preventing an echo loop. Envelope shape is a locked cross-repo contract —
see docs/sync_contract.md and sync/handlers.py on the Moonieful side, which
require a top-level `moonieful_client_id` and the event-specific fields
nested under `data`.

The stage receiver has moved twice. It started on Project, moved to
ClientProfile when the stage field was consolidated there (2026-05-25),
and now sits on Website — which is where a build's stage actually lives.
The middle position had a defect the cutover exposed: an account owning
two builds has two stages, and a client-level receiver could only ever
report one of them, so a stage change on the second site was never sent
to Moonieful at all.
"""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from clients.account_models import Website
from sync.models import SyncJob

logger = logging.getLogger(__name__)


def _enqueue(account, event_type, data):
    """Queue an outbound SyncJob, but only for accounts Moonieful actually
    knows about — otherwise every stage/maintenance change on every
    Aspired-direct client (the majority) would queue a job that can only
    ever fail on Moonieful's end with 'missing moonieful_client_id'."""
    if not account or not account.moonieful_client_id:
        return
    snapshot = {
        'moonieful_client_id': str(account.moonieful_client_id),
        'data': data,
    }
    job = SyncJob.objects.create(
        target='moonieful',
        account_new=account,
        event_type=event_type,
        payload=snapshot,
        payload_snapshot=snapshot,
    )
    logger.info('sync: queued %s SyncJob %s for account %s',
                event_type, job.pk, account.pk)


@receiver(pre_save, sender=Website)
def _stash_old_website_state(sender, instance, **kwargs):
    """Record the pre-save stage/maintenance_active so post_save can detect
    a transition in either."""
    if instance._state.adding or not instance.pk:
        instance._old_stage = None
        instance._old_maintenance_active = None
        return
    old = (
        Website.objects.filter(pk=instance.pk)
        .values('stage', 'maintenance_active')
        .first()
    )
    instance._old_stage = old['stage'] if old else None
    instance._old_maintenance_active = old['maintenance_active'] if old else None


@receiver(post_save, sender=Website)
def _queue_website_changes(sender, instance, created, **kwargs):
    """Queue outbound SyncJobs when a website's stage or maintenance_active
    flag changes."""
    if kwargs.get('raw'):
        return  # fixture load — never push restored data at Moonieful
    if getattr(instance, '_from_sync', False):
        return  # change came from inbound sync — do not echo it back
    if created:
        return

    account = instance.account

    old_stage = getattr(instance, '_old_stage', None)
    if old_stage is not None and old_stage != instance.stage:
        _enqueue(account, 'stage_changed',
                 {'aspired_project_stage': instance.stage})

    old_maint = getattr(instance, '_old_maintenance_active', None)
    if old_maint is not None and old_maint != instance.maintenance_active:
        _enqueue(account, 'maintenance_activated',
                 {'maintenance_active': instance.maintenance_active})
