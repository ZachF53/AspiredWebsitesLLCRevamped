"""
Auto-create Account + Website rows on new ClientProfile creation.

Phase C follow-up — keeps the Account/Website rows in sync with the
legacy ClientProfile during the transition. Every place that creates
a ClientProfile (Stripe onboarding webhook, Moonieful sync, admin
manual create, vault placeholder) goes through this signal, so the
new admin views and the per-website chooser see new clients without
a manual `refactor_to_accounts` run.

Phase D will reverse the dependency — ClientProfile creation goes
away entirely, and Account / Website are the only writes.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .account_models import Account, Website, _slugify_unique
from .models import ClientProfile

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ClientProfile)
def autocreate_account_and_website(sender, instance, created, **kwargs):
    """
    On new ClientProfile creation, materialise:
      1. An Account (1:1 with the user), pre-filled from the legacy
         profile so the WHOIS contact + Stripe customer ID propagate.
      2. One Website under that Account, unless this is a vault-only
         placeholder profile (no firm_name, no email, no business
         intent).

    Idempotent — get_or_create both sides keyed on the legacy FK so a
    re-fired signal (rare, but possible during data migrations) never
    duplicates.

    Signals re-firing on every save() are a footgun — gating on
    `created` only handles the first save. Plus a `getattr` flag check
    so the `refactor_to_accounts` backfill can opt out by setting
    ``instance._skip_autocreate = True`` before save().
    """
    if not created:
        return
    if getattr(instance, '_skip_autocreate', False):
        return

    try:
        account, acc_created = Account.objects.get_or_create(
            legacy_client_profile=instance,
            defaults={
                'user': instance.user,
                'name': instance.firm_name or (
                    instance.user.email if instance.user_id else ''),
                'contact_name': instance.contact_name or '',
                'phone': instance.phone or '',
                'address': instance.address or '',
                'city': instance.city or '',
                'state': instance.state or '',
                'zip_code': instance.zip_code or '',
                'country': 'US',
                'status': instance.status or 'active',
                'is_tester': bool(instance.is_tester),
                'stripe_customer_id': instance.stripe_customer_id or '',
                'preferred_contact_method': (
                    instance.preferred_contact_method or 'email'),
                'notify_on_stage_change': bool(
                    instance.notify_on_stage_change),
                'onboarding_status': 'pending_setup',
                'onboarding_complete': bool(instance.onboarding_complete),
                'moonieful_client_id': instance.moonieful_client_id,
                'synced_from_moonieful': bool(
                    instance.synced_from_moonieful),
            },
        )
        if acc_created:
            logger.info(
                'clients: auto-created Account %s for new ClientProfile %s',
                account.pk, instance.pk)
    except Exception:
        # Never block ClientProfile creation over an Account write
        # failure. The backfill command can pick up the slack.
        logger.exception(
            'clients: failed to auto-create Account for ClientProfile %s',
            instance.pk)
        return

    # Skip Website creation for vault-only placeholders — same rule
    # the backfill command uses. These profiles exist only to hold
    # credentials in the admin vault.
    has_intent = bool(
        instance.firm_name or instance.website or instance.package)
    if not has_intent:
        return

    try:
        Website.objects.get_or_create(
            account=account,
            legacy_project__isnull=True,
            name=instance.firm_name,
            defaults={
                'slug': _slugify_unique(
                    instance.firm_name or 'website', Website),
                'business_type': instance.business_type or '',
                'url': instance.website or '',
                'stage': instance.stage or 'intake',
                'package': instance.package or '',
                'onboarding_status': 'pending_intake',
            },
        )
    except Exception:
        logger.exception(
            'clients: failed to auto-create Website for ClientProfile %s',
            instance.pk)
