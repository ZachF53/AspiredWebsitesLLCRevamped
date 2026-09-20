"""
Start a recurring maintenance/social subscription for a Website.

Shared by the **go-Live** trigger (auto-start the plans the client opted
into when booking) and the operator **Add plan** button. Behaviour:

- If the customer has a **card on file** → create the subscription and let
  Stripe charge it now. Plan → ``active``.
- If **no card** → NO Stripe subscription is created yet. The plan is
  tagged ``awaiting_payment`` and the caller sends the client to our own
  ``/plan-pay/<plan_id>/`` page (billing/views.py) to enter a card via
  Stripe Elements. ``complete_awaiting_plan_payment`` below does the
  actual subscription creation once that card exists.

  This used to create the subscription immediately with
  ``collection_method='send_invoice'``, which let Stripe email its own
  hosted payment page — off our domain, and (in test mode) silently
  undeliverable to anyone who isn't a verified sender or team member.
  Worse: a ``send_invoice`` subscription is ``active`` in Stripe's own
  ``subscription.status`` the instant it's created, so the
  self-checkout webhook backstop (billing/webhooks.py) raced it and
  marked the plan paid before the client had done anything. Not
  creating the subscription until a card actually exists removes that
  race at the root instead of patching around it.

Discounts: the 10%-off-first-month opt-in promise (``honor_optin_10``) or an
operator-set custom ``discount_percent`` for ``once`` or ``forever``. Stored
on the plan row even in the awaiting_payment case so
``complete_awaiting_plan_payment`` applies the same coupon later.
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


def _activate_plan(plan, website, service_type, sub_id):
    """Common tail for "a Stripe subscription now exists and is charging":
    stamp the plan + (for maintenance) mirror the id onto the website.
    Used by complete_awaiting_plan_payment; start_website_plan's own
    card-on-file branch keeps its inline version since that path is
    unchanged and already covered by existing tests."""
    plan.stripe_subscription_id = sub_id
    plan.status = 'active'
    if not plan.started_at:
        plan.started_at = timezone.now()
    plan.save()
    if service_type == 'maintenance' and website is not None:
        website.maintenance_active = True
        website.maintenance_started_at = timezone.now()
        website.stripe_maintenance_subscription_id = sub_id
        website.save(update_fields=[
            'maintenance_active', 'maintenance_started_at',
            'stripe_maintenance_subscription_id', 'updated_at'])


def start_website_plan(website, service_type, tier_slug, *,
                       discount_percent=None, discount_duration='once',
                       honor_optin_10=False):
    """Create (or queue) the maintenance/social subscription for a
    Website. Returns the plan row (``active`` or ``awaiting_payment``),
    or None if it couldn't start (unknown tier, no Stripe price, Stripe
    failure). See module docstring for the awaiting_payment flow."""
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

    if discount_percent:
        plan.discount_percent = int(discount_percent)
        plan.discount_duration = (
            discount_duration if discount_duration in ('once', 'forever')
            else 'once')

    stripe = _stripe()
    customer_id = _customer_id_for(website) or _create_customer(stripe, website)
    if not customer_id:
        return None

    if not _has_card_on_file(stripe, customer_id):
        # No Stripe subscription yet — complete_awaiting_plan_payment
        # creates it once the client supplies a card on our own
        # /plan-pay/<plan_id>/ page. discount_percent/duration are
        # already saved above so that function applies the same coupon.
        plan.status = 'awaiting_payment'
        plan.save()
        return plan

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
    except Exception:
        logger.exception(
            'plan_billing: subscription create failed for website %s',
            website.pk)
        return None

    if service_type == 'maintenance' and plan.status == 'active':
        website.maintenance_active = True
        website.maintenance_started_at = timezone.now()
        website.stripe_maintenance_subscription_id = plan.stripe_subscription_id
        website.save(update_fields=[
            'maintenance_active', 'maintenance_started_at',
            'stripe_maintenance_subscription_id', 'updated_at'])
    return plan


def complete_awaiting_plan_payment(plan, payment_method_id):
    """Client supplied a card on our own /plan-pay/<plan_id>/ page for a
    plan that start_website_plan left at awaiting_payment (no card on
    file at Add Plan time). Creates the real Stripe subscription now,
    confirming its first invoice's PaymentIntent with the card just
    given — mirrors billing.checkout_views' self-checkout confirm flow
    (payment_behavior=default_incomplete, then confirm server-side) so
    SCA/3DS is handled the same proven way.

    Returns a dict:
      {'ok': True}                                    — charged, plan active
      {'requires_action': True, 'client_secret': ...} — browser must
                                                          finish SCA via
                                                          stripe.confirmCardPayment
      {'error': '...'}                                 — buyer-facing message

    Never raises. Idempotent: a plan that's already active (e.g. a
    double form submit) short-circuits to {'ok': True} without creating
    a second subscription.
    """
    from billing.checkout_views import _stripe_error_message
    from billing.pricing_models import ServiceTier
    from clients.service_models import MaintenancePlan

    if plan.status != 'awaiting_payment':
        return {'ok': True}

    website = plan.website
    account = plan.account
    service_type = 'maintenance' if isinstance(plan, MaintenancePlan) else 'social'
    category = _CATEGORY[service_type]

    tier = ServiceTier.objects.filter(
        slug=plan.tier_slug, category=category, is_active=True).first()
    if tier is None or not tier.stripe_price_id:
        return {'error': 'This plan is no longer available — contact us.'}

    stripe = _stripe()
    customer_id = (getattr(account, 'stripe_customer_id', '') or '') if account else ''
    if not customer_id and website is not None:
        customer_id = _create_customer(stripe, website)
    if not customer_id:
        return {'error': 'Could not find your account — contact us.'}

    try:
        stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
    except Exception as exc:
        logger.exception(
            'plan_billing: pay-page PM attach failed for plan %s', plan.pk)
        return {'error': _stripe_error_message(exc)}

    coupon = None
    if plan.discount_percent:
        coupon = ensure_percent_coupon(
            stripe, plan.discount_percent, plan.discount_duration or 'once')

    params = {
        'customer': customer_id,
        'items': [{'price': tier.stripe_price_id}],
        'payment_behavior': 'default_incomplete',
        'payment_settings': {'save_default_payment_method': 'on_subscription'},
        'expand': ['latest_invoice.confirmation_secret'],
        'metadata': {
            'website_id': str(website.id) if website else '',
            'product_type': category,
            'tier_slug': plan.tier_slug,
        },
    }
    if coupon:
        params['discounts'] = [{'coupon': coupon}]

    try:
        sub = stripe.Subscription.create(**params)
    except Exception as exc:
        logger.exception(
            'plan_billing: pay-page subscription create failed for plan %s',
            plan.pk)
        return {'error': _stripe_error_message(exc)}

    invoice = sub['latest_invoice'] if 'latest_invoice' in sub else None
    client_secret = None
    if invoice is not None and 'confirmation_secret' in invoice:
        cs = invoice['confirmation_secret']
        if cs and 'client_secret' in cs:
            client_secret = cs['client_secret']

    status = None
    if client_secret:
        payment_intent_id = client_secret.split('_secret', 1)[0]
        try:
            intent = stripe.PaymentIntent.confirm(
                payment_intent_id, payment_method=payment_method_id)
            status = intent['status'] if 'status' in intent else None
        except Exception as exc:
            logger.exception(
                'plan_billing: pay-page PI confirm failed for plan %s',
                plan.pk)
            return {'error': _stripe_error_message(exc)}

    if status == 'requires_action':
        return {'requires_action': True, 'client_secret': client_secret}

    _activate_plan(plan, website, service_type, sub.id)
    return {'ok': True}
