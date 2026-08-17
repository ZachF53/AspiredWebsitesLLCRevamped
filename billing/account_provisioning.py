"""
Shared self-checkout account provisioning.

A buyer who completes the custom Stripe Elements checkout needs a real
Django account (User + Account + the service-plan row) so they can be
dropped straight onto the set-password screen and into their dashboard. This used to live only in the
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

    ``Account.onboarding_status`` is intentionally left at its default
    so a subscription buyer with no website build lands straight in the
    dashboard — they are NOT pushed through the website intake gate.
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

    # The Account is created directly. This used to create a
    # ClientProfile, rely on a post_save signal to materialise the Account
    # behind it, and then call ensure_account to repair the result when
    # that signal had swallowed its own failure — three steps to produce
    # one row, with a paid-up customer facing a 500 if any of them lost.
    #
    # Self-checkout subscription buyers are NOT website-build clients, so
    # no Website is created: they would otherwise land on build-only nav
    # for a build they never bought.
    try:
        from clients.account_models import Account

        account, created = Account.objects.get_or_create(
            user=user,
            defaults={
                'name': derived_name,
                'contact_name': derived_name,
                'status': 'active',
                'stripe_customer_id': customer_id,
            },
        )
        if not created and not account.stripe_customer_id:
            account.stripe_customer_id = customer_id
            account.save(update_fields=[
                'stripe_customer_id', 'updated_at'])
    except Exception:  # noqa: BLE001
        logger.exception(
            'provision: Account create failed for %s', user.pk)
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='error',
                source='billing.provision.profile_create',
                message=f'Account auto-create failed for {email}',
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
        from clients.service_models import MaintenancePlan, SocialMediaPlan
        from billing.pricing_models import ServiceTier

        account = Account.objects.filter(user=user).first()
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
                # Mirror onto the sites the plan covers. Maintenance is
                # sold per site, so a buyer with no website build has
                # nothing to mirror onto — the MaintenancePlan row above
                # is the record of what they bought either way.
                for site in account.websites.all():
                    site.stripe_maintenance_subscription_id = subscription_id
                    site.maintenance_active = True
                    if not site.maintenance_started_at:
                        site.maintenance_started_at = timezone.now()
                    site.package = tier_slug.replace('-', '_')
                    site.save(update_fields=[
                        'stripe_maintenance_subscription_id',
                        'maintenance_active', 'maintenance_started_at',
                        'package', 'updated_at'])
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
                # Social is account-level, matching where the plan is
                # sold and where its channels post from.
                if account.stripe_social_subscription_id != subscription_id:
                    account.stripe_social_subscription_id = subscription_id
                    account.save(update_fields=[
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
