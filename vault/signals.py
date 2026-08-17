"""Vault signals — auto-create a ClientVault, seed SSH default commands."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from clients.account_models import Account

from .models import ClientVault, VaultCredential

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Account)
def create_client_vault(sender, instance, created, **kwargs):
    """Every account — whether from Stripe or Moonieful sync — gets a vault.

    Fires on Account rather than ClientProfile. Both are created for a new
    client today, so the vault still appears exactly once and at the same
    moment; what changes is that an account created *without* a legacy
    profile now gets one too. Under the old receiver it did not, and the
    absence only surfaced later as a
    ``vault.ClientVault.missing-canonical-account`` parity finding, or as
    a client whose credentials page had nothing behind it.

    Keyed on the Account, so re-running against a client that already has
    a vault adopts it rather than colliding on the unique FK.
    """
    if kwargs.get('raw') or not created:
        return

    vault = ClientVault.objects.filter(account_new=instance).first()
    if vault is None:
        # A vault may already exist against the legacy profile from an
        # earlier creation path; adopt it rather than creating a second.
        legacy = instance.legacy_client_profile
        if legacy is not None:
            vault = ClientVault.objects.filter(client=legacy).first()

    if vault is None:
        ClientVault.objects.create(
            account_new=instance,
            client=instance.legacy_client_profile,
        )
        logger.info('vault: created ClientVault for account %s', instance.pk)
        return

    if vault.account_new_id is None:
        vault.account_new = instance
        vault.save(update_fields=['account_new', 'updated_at'])
        logger.info(
            'vault: linked existing ClientVault %s to account %s',
            vault.pk, instance.pk)


@receiver(post_save, sender=VaultCredential)
def seed_ssh_default_commands(sender, instance, **kwargs):
    """Seed the default command library for an SSH credential that has none."""
    if kwargs.get('raw'):
        return
    if instance.is_ssh_credential and not instance.commands.exists():
        from .default_commands import create_default_commands
        create_default_commands(instance)
