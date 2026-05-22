"""Auto-create a ClientVault whenever a ClientProfile is created."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from clients.models import ClientProfile

from .models import ClientVault

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ClientProfile)
def create_client_vault(sender, instance, created, **kwargs):
    """Every client — whether from Stripe or Moonieful sync — gets a vault."""
    if not created:
        return
    ClientVault.objects.get_or_create(client=instance)
    logger.info('vault: created ClientVault for %s', instance.pk)
