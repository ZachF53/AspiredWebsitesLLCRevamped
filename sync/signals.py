"""
Outbound sync signals (Aspired → Moonieful).

When a Website's stage changes locally, queue a SyncJob so run_sync can
notify Moonieful. Changes that originated from an *inbound* sync carry
`instance._from_sync = True` and are skipped, preventing an echo loop.

The receiver has moved twice. It started on Project, moved to
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


@receiver(pre_save, sender=Website)
def _stash_old_stage(sender, instance, **kwargs):
    """Record the pre-save stage so post_save can detect a transition."""
    if instance._state.adding or not instance.pk:
        instance._old_stage = None
        return
    instance._old_stage = (
        Website.objects.filter(pk=instance.pk)
        .values_list('stage', flat=True)
        .first()
    )


@receiver(post_save, sender=Website)
def _queue_stage_change(sender, instance, created, **kwargs):
    """Queue an outbound SyncJob when a website's stage changes."""
    if kwargs.get('raw'):
        return  # fixture load — never push restored data at Moonieful
    if getattr(instance, '_from_sync', False):
        return  # change came from inbound sync — do not echo it back
    if created:
        return
    old_stage = getattr(instance, '_old_stage', None)
    if old_stage is None or old_stage == instance.stage:
        return

    account = instance.account
    # Moonieful identifies clients by their own id, which is an
    # account-level fact; the stage that changed is the site's.
    snapshot = {
        'client_id': str(account.id) if account else None,
        'website_id': str(instance.id),
        'from_stage': old_stage,
        'to_stage': instance.stage,
    }
    SyncJob.objects.create(
        target='moonieful',
        account_new=account,
        website_new=instance,
        moonieful_client_id=(
            account.moonieful_client_id if account else None),
        event_type='stage_changed',
        payload=snapshot,
        payload_snapshot=snapshot,
    )
    logger.info(
        'sync: queued stage_changed SyncJob for website %s', instance.pk)
