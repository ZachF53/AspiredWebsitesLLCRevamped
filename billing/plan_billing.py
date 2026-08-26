"""
Start a recurring maintenance/social subscription for a Website.

Shared by the **go-Live** trigger (auto-start the plans the client opted
into when booking) and the operator **Add plan** button. Behaviour:

- If the customer has a **card on file** → create the subscription and let
  Stripe charge it now. Plan → ``active``.
- If **no card** → create the subscription with ``collection_method=
  'send_invoice'`` so Stripe emails a hosted payment link; the plan is
  tagged ``awaiting_payment`` until ``invoice.paid`` clears it (see
  billing/webhooks.py).

Discounts: the 10%-off-first-month opt-in promise (``honor_optin_10``) or an
operator-set custom ``discount_percent`` for ``once`` or ``forever``.
Never raises — billing hiccups must not break a stage change.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# service_type → ServiceTier.category
_CATEGORY = {'maintenance': 'maintenance', 'social': 'social_media'}


def _stripe():
    import stripe

    from billing.stripe_helpers import _init
    _init()
    return stripe


def _customer_id_for(website):
    """The Stripe customer holding the saved card, off the Account.

    The card and the billing relationship are account-level, and the
    cutover contract puts `stripe_customer_id` on Account. This read the
    legacy profile's copy first and fell back to the Account, which meant
    the legacy row decided who got charged.

    Checked against production before the preference was flipped: all 10
    accounts hold the same id on both sides — no mismatches, and no
    account where only the legacy row carried one. So this is the same
    customer it was already resolving, read from the row that survives.
    """
    acct = website.account
    return (getattr(acct, 'stripe_customer_id', '') or '') if acct else ''


def _create_customer(stripe, website):
    """Create a Stripe customer for the account + persist its id."""
    acct = website.account
    email = (acct.user.email if (acct and acct.user_id) else '') or ''
    try:
        cust = stripe.Customer.create(
            email=email, name=(acct.name if acct else ''))
    except Exception:
        logger.exception('plan_billing: customer create failed')
        return ''
    if acct is not None:
        acct.stripe_customer_id = cust.id
        acct.save(update_fields=['stripe_customer_id'])
    return cust.id


def _has_card_on_file(stripe, customer_id):
    if not customer_id:
        return False
    try:
        cust = stripe.Customer.retrieve(customer_id)
        try:
            if cust.invoice_settings.default_payment_method:
                return True
        except Exception:
            pass
        pms = stripe.PaymentMethod.list(
            customer=customer_id, type='card', limit=1)
        return bool(pms.data)
    except Exception:
        logger.exception('plan_billing: card check failed for %s', customer_id)
        return False


def ensure_percent_coupon(stripe, percent, duration):
    """get-or-create a reusable coupon for (percent, duration). duration is
    'once' or 'forever'. Returns the coupon id or None."""
    duration = duration if duration in ('once', 'forever') else 'once'
    cid = f'pct{int(percent)}_{duration}'
    try:
        stripe.Coupon.retrieve(cid)
        return cid
    except Exception:
        try:
            stripe.Coupon.create(
                id=cid, percent_off=int(percent), duration=duration,
                name=f'{int(percent)}% off ({duration})')
            return cid
        except Exception:
            logger.exception('plan_billing: coupon ensure failed %s', cid)
            return None


def start_website_plan(website, service_type, tier_slug, *,
                       discount_percent=None, discount_duration='once',
                       honor_optin_10=False):
    """Create the maintenance/social subscription for a Website. Returns the
    plan row (``active`` or ``awaiting_payment``), or None if it couldn't
    start (unknown tier, no Stripe price, Stripe failure)."""
    from billing.pricing_models import ServiceTier
    from clients.service_models import MaintenancePlan, SocialMediaPlan

    category = _CATEGORY.get(service_type)
    if category is None:
        return None
    tier = ServiceTier.objects.filter(
        slug=tier_slug, category=category, is_active=True).first()
    if tier is None or not tier.stripe_price_id:
        logger.warning(
            'plan_billing: tier %r missing or has no Stripe price', tier_slug)
        return None

    account = website.account
    Model = MaintenancePlan if service_type == 'maintenance' else SocialMediaPlan
    plan, _created = Model.objects.get_or_create(
        account=account, website=website,
        defaults={'tier_slug': tier_slug, 'status': 'paused'})
    # Already actively billing → don't double-charge.
    if plan.status == 'active' and plan.stripe_subscription_id:
        return plan
    plan.tier_slug = tier_slug

    stripe = _stripe()
    customer_id = _customer_id_for(website) or _create_customer(stripe, website)
    if not customer_id:
        return None

    coupon = None
    if honor_optin_10:
        from billing.checkout_views import _ensure_addon_firstmonth_coupon
        coupon = _ensure_addon_firstmonth_coupon(stripe)
    elif discount_percent:
        coupon = ensure_percent_coupon(
            stripe, discount_percent, discount_duration)

    params = {
        'customer': customer_id,
        'items': [{'price': tier.stripe_price_id}],
        'metadata': {
            'website_id': str(website.id),
            'product_type': category,
            'tier_slug': tier_slug,
        },
    }
    if coupon:
        params['discounts'] = [{'coupon': coupon}]

    try:
        if _has_card_on_file(stripe, customer_id):
            sub = stripe.Subscription.create(**params)
            plan.stripe_subscription_id = sub.id
            plan.status = 'active'
            plan.started_at = timezone.now()
            # Commit the subscription id NOW. Stripe fires
            # customer.subscription.created the instant the subscription
            # exists, and that webhook looks this plan up by subscription
            # id. Leaving the id in memory until the save at the end of
            # this function gave the webhook an empty result, so it
            # created a SECOND, website-less plan row for the same
            # purchase. Persisting here closes that window.
            plan.save()
        else:
            params['collection_method'] = 'send_invoice'
            params['days_until_due'] = 7
            params['expand'] = ['latest_invoice']
            sub = stripe.Subscription.create(**params)
            plan.stripe_subscription_id = sub.id
            plan.status = 'awaiting_payment'
            # Same race, and worse here: finalize + send below are two
            # more blocking Stripe round-trips, which is exactly the gap
            # the webhook was winning.
            plan.save()
            inv = sub.latest_invoice if hasattr(sub, 'latest_invoice') else None
            inv_id = getattr(inv, 'id', None) or (
                inv if isinstance(inv, str) else None)
            if inv_id:
                try:
                    stripe.Invoice.finalize_invoice(inv_id)
                    stripe.Invoice.send_invoice(inv_id)
                except Exception:
                    logger.exception('plan_billing: send invoice failed')
                plan.awaiting_invoice_id = inv_id
    except Exception:
        logger.exception(
            'plan_billing: subscription create failed for website %s',
            website.pk)
        return None

    if discount_percent:
        plan.discount_percent = int(discount_percent)
        plan.discount_duration = (
            discount_duration if discount_duration in ('once', 'forever')
            else 'once')
    plan.save()

    if service_type == 'maintenance' and plan.status == 'active':
        website.maintenance_active = True
        website.maintenance_started_at = timezone.now()
        website.stripe_maintenance_subscription_id = plan.stripe_subscription_id
        website.save(update_fields=[
            'maintenance_active', 'maintenance_started_at',
            'stripe_maintenance_subscription_id', 'updated_at'])
    return plan
