"""
Shared self-checkout account provisioning.

A buyer who completes the custom Stripe Elements checkout needs a real
Django account (User + ClientProfile + Account + the Phase-D service-plan
row) so they can be dropped straight onto the set-password screen and
into their dashboard. This used to live only in the
``customer.subscription.created`` webhook — but that webhook is async and
was not reliably firing, so the buyer ended up paid-but-account-less.

This module is the single source of truth. The synchronous checkout view
calls it the instant the charge succeeds; the webhook calls it too as an
idempotent backstop. Every write is get_or_create / update_or_create, so
running both paths for the same purchase is a no-op after the first.
"""

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone

logger = logging.getLogger(__name__)


def provision_self_checkout_account(*, email, customer_id, tier_slug,
                                    product_type, subscription_id='',
                                    hosting_upsell=False, customer_name=''):
    """Idempotently create the account objects for a self-checkout sale.

    Returns the ``User`` (with a possibly-unusable password — the buyer
    sets it on the set-password screen), or ``None`` if no email.

    ``ClientProfile.onboarding_status`` is intentionally left at its
    default (``onboarding_complete``) so a subscription buyer with no
    website build lands straight in the dashboard — they are NOT pushed
    through the website intake gate.
    """
    User = get_user_model()
    email = (email or '').strip().lower()
    if not email:
        return None

    user, created = User.objects.get_or_create(
        email__iexact=email,
        defaults={'username': email, 'email': email, 'is_active': True},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])

    derived_name = (customer_name or '').strip()
    if not derived_name:
        local = email.split('@', 1)[0]
        derived_name = local.replace('.', ' ').replace('-', ' ').title()

    # ClientProfile is the legacy "everything" model the portal still
    # resolves against; Account is the Phase-C login-level model (the
    # post_save signal usually creates it, but we get_or_create to be safe).
    try:
        from clients.models import ClientProfile
        from clients.account_models import Account

        cp, cp_created = ClientProfile.objects.get_or_create(
            user=user,
            defaults={
                'firm_name': derived_name,
                'contact_name': derived_name,
                'status': 'active',
                'stripe_customer_id': customer_id,
            },
        )
        if not cp_created and not cp.stripe_customer_id:
            cp.stripe_customer_id = customer_id
            cp.save(update_fields=['stripe_customer_id'])

        Account.objects.get_or_create(
            user=user,
            defaults={
                'name': derived_name,
                'status': 'active',
                'stripe_customer_id': customer_id,
                'legacy_client_profile': cp,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            'provision: ClientProfile/Account create failed for %s',
            user.pk)
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='error',
                source='billing.provision.profile_create',
                message=f'ClientProfile/Account auto-create failed for {email}',
                detail='Customer paid but portal will 500 — investigate.',
            )
        except Exception:
            pass

    # Onboarding record — drives the in-dashboard onboarding checklist.
    try:
        from onboarding.models import Onboarding
        Onboarding.objects.get_or_create(
            user=user, product_type=product_type, tier_slug=tier_slug,
        )
    except Exception:  # noqa: BLE001
        logger.exception('provision: Onboarding create failed for %s', user.pk)

    # Phase-D service-plan row — the canonical record of what they bought.
    try:
        from clients.account_models import Account
        from clients.models import ClientProfile
        from clients.service_models import MaintenancePlan, SocialMediaPlan
        from billing.pricing_models import ServiceTier

        account = Account.objects.filter(user=user).first()
        cp = ClientProfile.objects.filter(user=user).first()
        if account is not None and subscription_id:
            if product_type == 'maintenance':
                MaintenancePlan.objects.update_or_create(
                    account=account,
                    stripe_subscription_id=subscription_id,
                    defaults={
                        'tier_slug': tier_slug,
                        'status': 'active',
                        'started_at': timezone.now(),
                        'hosting_move_over': bool(hosting_upsell),
                        'hosting_move_over_at': (
                            timezone.now() if hosting_upsell else None),
                    },
                )
                # Mirror onto the legacy ClientProfile fields the portal
                # still reads everywhere (subscriptions list, upsell
                # state, dashboard maintenance card, cancel/resume). The
                # new MaintenancePlan row above is the Phase-D source of
                # truth; these keep the not-yet-migrated views working.
                # This is an UPDATE (cp already exists) so it does NOT
                # re-fire the Website-autocreate signal (created=False).
                if cp is not None:
                    cp.stripe_subscription_id = subscription_id
                    cp.maintenance_active = True
                    if not cp.maintenance_started_at:
                        cp.maintenance_started_at = timezone.now()
                    cp.package = tier_slug.replace('-', '_')
                    cp.save(update_fields=[
                        'stripe_subscription_id', 'maintenance_active',
                        'maintenance_started_at', 'package', 'updated_at'])
            elif product_type == 'social_media':
                tier = ServiceTier.objects.filter(slug=tier_slug).first()
                max_ch = (tier.max_channels if tier and tier.max_channels
                          else 2)
                SocialMediaPlan.objects.update_or_create(
                    account=account,
                    stripe_subscription_id=subscription_id,
                    defaults={
                        'tier_slug': tier_slug,
                        'status': 'active',
                        'started_at': timezone.now(),
                        'max_channels': max_ch,
                    },
                )
                # Legacy mirror so the subscriptions list shows the social
                # sub for not-yet-migrated views (package enum has no
                # social value, so only the pointer field is set).
                if cp is not None and (
                        cp.stripe_social_subscription_id != subscription_id):
                    cp.stripe_social_subscription_id = subscription_id
                    cp.save(update_fields=[
                        'stripe_social_subscription_id', 'updated_at'])
    except Exception:  # noqa: BLE001
        logger.exception(
            'provision: service-model write failed for %s', user.pk)

    return user


def issue_password_setup_link(user, send_email=False):
    """Mint a fresh PasswordSetupToken and return its setup URL path
    (``/set-password/<token>/``). Optionally email the magic link too as
    a backstop in case the buyer closes the tab before setting a password.
    """
    from onboarding.password_models import PasswordSetupToken

    token = PasswordSetupToken.create_for(user)
    if send_email:
        try:
            from onboarding.password_views import send_password_setup_email
            send_password_setup_email(user, token)
        except Exception:  # noqa: BLE001
            logger.exception(
                'provision: password-setup email failed for %s', user.pk)
    return f'/set-password/{token.token}/'
