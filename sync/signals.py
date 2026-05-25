"""
Outbound sync signals (Aspired → Moonieful).

When a ClientProfile's stage changes locally, queue a SyncJob so
run_sync can notify Moonieful. Changes that originated from an
*inbound* sync carry `instance._from_sync = True` and are skipped,
preventing an echo loop.

Post-2026-05-25 refactor: this signal moved from Project to
ClientProfile because the stage field now lives on the client.
"""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from clients.models import ClientProfile
from sync.models import SyncJob

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=ClientProfile)
def _stash_old_stage(sender, instance, **kwargs):
    """Record the pre-save stage so post_save can detect a transition."""
    if instance._state.adding or not instance.pk:
        instance._old_stage = None
        return
    instance._old_stage = (
        ClientProfile.objects.filter(pk=instance.pk)
        .values_list('stage', flat=True)
        .first()
    )


@receiver(post_save, sender=ClientProfile)
def _queue_stage_change(sender, instance, created, **kwargs):
    """Queue an outbound SyncJob when a client's stage changes."""
    if getattr(instance, '_from_sync', False):
        return  # change came from inbound sync — do not echo it back
    if created:
        return
    old_stage = getattr(instance, '_old_stage', None)
    if old_stage is None or old_stage == instance.stage:
        return

    snapshot = {
        'client_id': str(instance.id),
        'from_stage': old_stage,
        'to_stage': instance.stage,
    }
    SyncJob.objects.create(
        target='moonieful',
        client=instance,
        moonieful_client_id=instance.moonieful_client_id,
        event_type='stage_changed',
        payload=snapshot,
        payload_snapshot=snapshot,
    )
    logger.info(
        'sync: queued stage_changed SyncJob for client %s', instance.pk)
