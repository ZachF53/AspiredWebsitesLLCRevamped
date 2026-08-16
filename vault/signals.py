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
    # Restoring a fixture (raw=True) already carries the ClientVault rows;
    # creating another would collide with the unique client FK.
    if kwargs.get('raw') or not created:
        return
    try:
        account = instance.migrated_account
    except Exception:
        account = None
    vault, _ = ClientVault.objects.get_or_create(
        client=instance,
        defaults={'account_new': account},
    )
    # Another creation path may already have materialised the vault without
    # its transitional canonical FK; repair that row instead of duplicating it.
    if account is not None and vault.account_new_id is None:
        vault.account_new = account
        vault.save(update_fields=['account_new', 'updated_at'])
    logger.info('vault: created ClientVault for %s', instance.pk)


@receiver(post_save, sender=VaultCredential)
def seed_ssh_default_commands(sender, instance, **kwargs):
    """Seed the default command library for an SSH credential that has none."""
    if kwargs.get('raw'):
        return
    if instance.is_ssh_credential and not instance.commands.exists():
        from .default_commands import create_default_commands
        create_default_commands(instance)
