"""Vault signals — auto-create a ClientVault, seed SSH default commands."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from clients.models import ClientProfile

from .models import ClientVault, VaultCredential

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ClientProfile)
def create_client_vault(sender, instance, created, **kwargs):
    """Every client — whether from Stripe or Moonieful sync — gets a vault."""
    if not created:
        return
    ClientVault.objects.get_or_create(client=instance)
    logger.info('vault: created ClientVault for %s', instance.pk)


@receiver(post_save, sender=VaultCredential)
def seed_ssh_default_commands(sender, instance, **kwargs):
    """Seed the default command library for an SSH credential that has none."""
    if instance.is_ssh_credential and not instance.commands.exists():
        from .default_commands import create_default_commands
        create_default_commands(instance)
