"""
Canonical Account materialisation — cutover Wave 1.

Five code paths create a ``ClientProfile``: the admin's manual client create,
the admin's comped-client create, self-checkout provisioning, the booking
scheduler, and the vault's placeholder profile.  None of them create the
``Account`` themselves.  They all rely on
``clients.signals.autocreate_account_and_website``, which wraps its work in
``except Exception: logger.exception(...); return`` — deliberately, so an
Account write can never block a client from being created or a customer from
paying.

The cost of that choice is silent: when the signal does fail, the profile is
left with no Account at all.  Nothing surfaces it, the client portal resolves
against a missing row, and the parity audit reports
``client-profile-missing-account`` weeks later.  The migration rehearsal
carries this shape on purpose ("Delgado Injury Law") because production can
carry it too.

This module makes the linkage checkable instead of hopeful.  Callers create
their ClientProfile exactly as before, then call :func:`ensure_account`,
which verifies the invariant the cutover contract requires — *every legacy
ClientProfile has exactly one linked Account, referencing the same user* —
and repairs it when the signal did not.  It is idempotent, so calling it on
an already-healthy profile is a no-op, and it is safe to call from a webhook
that may run twice.

The canonical field list lives here too.  It was previously copy-pasted into
four modules (the signal, both backfills, and the parity validator); a field
added to one and missed in another shows up as permanent, unfixable drift in
the audit.
"""

import logging

logger = logging.getLogger(__name__)


# Account-level fields that exist on BOTH ClientProfile and Account with the
# same name. Build/per-site fields (stage, payment, revisions, droplet) are
# deliberately absent — those belong to Website.
ACCOUNT_LEVEL_FIELDS = [
    'contact_name', 'phone', 'address', 'city', 'state', 'zip_code',
    'status', 'is_tester', 'internal_notes', 'stripe_customer_id',
    'preferred_contact_method', 'notify_on_stage_change',
    'onboarding_complete',
    'client_pin_hash', 'client_pin_salt', 'client_pin_set',
    'client_pin_failed_attempts', 'client_pin_lockout_until',
    'moonieful_client_id', 'synced_from_moonieful', 'last_synced_at',
    'sync_conflict_flagged',
    'comp_build_package', 'comp_maintenance_package', 'comp_social_tier',
    'comp_notes',
    # Dunning + social billing. Added once it turned out neither had any
    # canonical home at all: the drop would have zeroed every client's
    # payment-failure history and lost the social subscription id.
    'payment_failure_started_at', 'payment_failure_offenses',
    'stripe_social_subscription_id',
]


class AccountSetupError(Exception):
    """The Account invariant cannot be satisfied without a human decision."""


def account_name_for(profile):
    """The display name an Account takes from its legacy profile.

    Decided 2026-08-16: ``Account.name`` is the client's billing / account
    organisation name — normally the firm name. The three names are
    distinct and must not be conflated:

        Account.name          the organisation that pays the bill
        Account.contact_name  the person
        Website.name          the individual brand or site

    In particular the Account name is NEVER derived from the first
    Website. An account holding "Vance Family Law" and "Vance Mediation
    Services" is billed as Vance Family Law the firm, not renamed by
    whichever site happens to be oldest.

    Falls back to the login email only when the legacy profile carries no
    firm name at all, so the account is never nameless.
    """
    return (profile.firm_name
            or (profile.user.email if profile.user_id else ''))


def account_onboarding_status_for(profile):
    """Map a legacy three-state onboarding status onto the Account's two.

    Account-level onboarding is only WHOIS contact + vault PIN; the intake
    form is per-Website. So a profile that has moved past account setup —
    ``pending_intake`` or ``onboarding_complete`` — is complete at the
    account level.

    The autocreate signal used to hardcode ``pending_setup`` for every new
    Account regardless of the profile's real state, which is why accounts
    created by the signal claim to be pending setup while their profile says
    onboarding is finished. Only an admin badge reads the field today, so
    the discrepancy has been invisible — but the portal gate is due to move
    onto this field in Wave 2, and it has to be true before it can be
    trusted.
    """
    legacy = getattr(profile, 'onboarding_status', '') or ''
    if legacy in ('pending_intake', 'onboarding_complete'):
        return 'complete'
    return 'pending_setup'


def account_defaults_from(profile):
    """Field values an Account inherits from its legacy ClientProfile."""
    values = {
        name: getattr(profile, name)
        for name in ACCOUNT_LEVEL_FIELDS
        if hasattr(profile, name)
    }
    values['name'] = account_name_for(profile)
    values['country'] = 'US'
    return values


def ensure_account(profile, *, repair_fields=False):
    """Return the Account for ``profile``, creating or relinking as needed.

    Resolution order:

    1. An Account already linked to this profile — the normal case, and the
       only one that does no writes.
    2. An Account owned by this profile's user but not yet linked to it.
       This is what a partially-failed signal leaves behind, and what
       ``provision_self_checkout_account`` produces when it reaches its
       ``Account.objects.get_or_create(user=...)`` fallback. Link it.
    3. Nothing exists. Create it from the profile.

    ``repair_fields`` additionally pulls current account-level values across
    from the profile. Off by default: at creation time the values are
    already identical, and on an established account the profile is not
    automatically the winner.

    Raises :class:`AccountSetupError` when an Account is linked to this
    profile but owned by a different user. That is a genuine conflict — one
    of the two users has the client's login history and the other does not —
    and picking wrong hands the wrong person a portal. Repair it explicitly
    with ``repair_account_website_parity``.
    """
    from clients.account_models import Account

    linked = Account.objects.filter(legacy_client_profile=profile).first()
    if linked is not None:
        if linked.user_id != profile.user_id:
            raise AccountSetupError(
                f'Account {linked.pk} is linked to ClientProfile '
                f'{profile.pk} but owned by user {linked.user_id}, not '
                f'{profile.user_id}. Resolve with '
                'repair_account_website_parity.')
        if repair_fields:
            _copy_fields(profile, linked)
        return linked

    by_user = Account.objects.filter(user_id=profile.user_id).first()
    if by_user is not None:
        by_user.legacy_client_profile = profile
        by_user.save(update_fields=['legacy_client_profile', 'updated_at'])
        logger.info(
            'clients: linked existing Account %s to ClientProfile %s',
            by_user.pk, profile.pk)
        _link_vault(profile, by_user)
        if repair_fields:
            _copy_fields(profile, by_user)
        return by_user

    account = Account.objects.create(
        user=profile.user,
        legacy_client_profile=profile,
        onboarding_status=account_onboarding_status_for(profile),
        **account_defaults_from(profile),
    )
    logger.warning(
        'clients: Account was missing for ClientProfile %s — created %s. '
        'The autocreate signal did not complete.',
        profile.pk, account.pk)
    _link_vault(profile, account)
    return account


def _link_vault(profile, account):
    """Point the profile's ClientVault at the Account.

    ``vault.signals.create_client_vault`` fires on the same post_save as the
    Account autocreate and reads ``profile.migrated_account`` to fill
    ``account_new``. When the Account signal fails or is skipped, the vault
    is still created — with a null Account — and the parity audit reports
    ``vault.ClientVault.missing-canonical-account``. Creating the Account
    late has to fix up the vault it left behind.
    """
    vault = getattr(profile, 'vault', None)
    if vault is None or vault.account_new_id == account.pk:
        return
    vault.account_new = account
    vault.save(update_fields=['account_new', 'updated_at'])
    logger.info(
        'clients: linked ClientVault %s to Account %s', vault.pk, account.pk)


def _copy_fields(profile, account):
    changed = [
        name for name in ACCOUNT_LEVEL_FIELDS
        if hasattr(profile, name) and hasattr(account, name)
        and getattr(account, name) != getattr(profile, name)
    ]
    name = account_name_for(profile)
    if name and account.name != name:
        account.name = name
        changed.append('name')
    if changed:
        for field in changed:
            if field != 'name':
                setattr(account, field, getattr(profile, field))
        account.save(update_fields=changed + ['updated_at'])
    return changed


def website_onboarding_status_for(profile):
    """The per-site intake state a new Website inherits from its profile.

    The autocreate signal hardcoded 'pending_intake' for every Website it
    made, while `refactor_to_accounts` derived it from the client's stage.
    The two disagreeing did not matter while only an admin badge read the
    field — but the portal gate reads it now, so a signal-created site
    would have bounced an established client into the intake form on
    every page.

    Mirrors the backfill exactly:
      live               -> complete       (the build shipped)
      pending_intake     -> pending_intake (they genuinely owe one)
      anything else      -> intake_complete
    """
    if (getattr(profile, 'stage', '') or '') == 'live':
        return 'complete'
    if (getattr(profile, 'onboarding_status', '') or '') == 'pending_intake':
        return 'pending_intake'
    return 'intake_complete'
