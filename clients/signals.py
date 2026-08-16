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
from .account_setup import (
    ACCOUNT_LEVEL_FIELDS,
    account_name_for,
    account_onboarding_status_for,
)
from .models import ClientProfile

logger = logging.getLogger(__name__)


# Account-level fields kept in sync ClientProfile -> Account on every CP
# update during the Phase-D transition, so Account stays the live source
# of truth even while some legacy code still writes the CP. One-way only
# (Account.save never re-saves the CP), so there's no signal loop.
#
# Defined once in clients.account_setup — this list used to be copy-pasted
# here, into both backfill commands, and into the parity validator. A field
# added to one copy and missed in another reads as permanent drift the
# audit can never clear.
_ACCOUNT_SYNC_FIELDS = ACCOUNT_LEVEL_FIELDS


def _sync_account_fields(cp):
    """Mirror account-level fields from a ClientProfile onto its Account."""
    try:
        acc = cp.migrated_account
    except Exception:
        acc = None
    if acc is None:
        return
    changed = []
    for f in _ACCOUNT_SYNC_FIELDS:
        if (hasattr(cp, f) and hasattr(acc, f)
                and getattr(acc, f) != getattr(cp, f)):
            setattr(acc, f, getattr(cp, f))
            changed.append(f)
    expected_name = account_name_for(cp)
    if expected_name and acc.name != expected_name:
        acc.name = expected_name
        changed.append('name')
    if changed:
        try:
            acc.save(update_fields=changed + ['updated_at'])
        except Exception:
            logger.exception(
                'CP->Account field sync failed for %s', cp.pk)


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
    # `raw=True` means a fixture is being loaded (loaddata / a restored
    # snapshot). The fixture already contains the Account and Website rows,
    # so running this would try to create a second Account for a user who
    # is about to get theirs from the fixture — and Account.user is a
    # OneToOne. Business logic must not fire while data is being restored.
    if kwargs.get('raw'):
        return
    if getattr(instance, '_skip_autocreate', False):
        return
    if not created:
        # Keep Account current with later CP edits (settings, PIN, billing)
        # until those writes move to Account directly.
        _sync_account_fields(instance)
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
                'onboarding_status': account_onboarding_status_for(
                    instance),
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
    # Subscription-only buyers (maintenance / social self-checkout) set
    # _skip_website_autocreate so they don't get a build Website — they'd
    # otherwise surface build-only nav (My Project / Intake / Revisions).
    has_intent = bool(
        instance.firm_name or instance.website or instance.package)
    if not has_intent or getattr(
            instance, '_skip_website_autocreate', False):
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
