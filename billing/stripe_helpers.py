"""
Stripe integration helpers — customers, build invoices, maintenance subs.

Every public function raises StripeNotConfigured if STRIPE_SECRET_KEY is unset,
so callers can degrade gracefully in development.
"""

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class StripeNotConfigured(RuntimeError):
    """Raised when a Stripe call is attempted without STRIPE_SECRET_KEY set."""


def _init():
    if not settings.STRIPE_SECRET_KEY:
        raise StripeNotConfigured('STRIPE_SECRET_KEY is not set in .env')
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _cents(amount):
    """Convert a dollar Decimal/number to integer cents."""
    return int((Decimal(amount) * 100).to_integral_value())


def create_or_get_customer(client):
    """
    Return the client's Stripe Customer, creating + storing it ONLY if
    we've never had one for them OR the existing one has been
    explicitly hard-deleted at Stripe.

    CRITICAL: this function must NEVER silently swap a stripe_customer_id
    on a transient error. Doing so orphans the customer's saved cards,
    invoices, and subscriptions — they all stay on the old Stripe
    customer but the DB now points elsewhere. From the client's POV
    their card "disappears" mid-flow.

    The old version used `customer.get('deleted')` to detect deletes,
    which raises AttributeError on Stripe Python v15 StripeObjects
    (no `.get` method). The bare `except Exception` swallowed that
    error and silently created a replacement on every single call.
    """
    _init()
    if client.stripe_customer_id:
        try:
            customer = stripe.Customer.retrieve(client.stripe_customer_id)
        except stripe.error.InvalidRequestError as exc:
            # Stripe returns 404 → InvalidRequestError. This is the
            # only safe-to-rotate case: the customer is genuinely
            # gone (manually purged in the dashboard, or never
            # existed in this Stripe environment, e.g. live vs test
            # mode mismatch).
            logger.error(
                'Stripe customer %s NOT FOUND for client %s — '
                'creating a replacement. Old customer\'s cards + '
                'history are orphaned in Stripe; manual recovery '
                'may be needed. Underlying error: %s',
                client.stripe_customer_id, client.pk, exc)
        except Exception:
            # ANY other failure (network, rate limit, parser quirk,
            # auth) — re-raise. We must NOT silently create a new
            # customer; that's exactly the bug that lost a card.
            logger.exception(
                'Stripe customer retrieval error for client %s — '
                'refusing to create a replacement (would orphan '
                'card/history); raising.', client.pk)
            raise
        else:
            # StripeObject inherits from dict so [] indexing works,
            # but `.get()` was removed in v15 — use getattr for safety.
            if not getattr(customer, 'deleted', False):
                return customer
            # Customer exists but is in deleted state — only path
            # where we fall through to recreation.
            logger.error(
                'Stripe customer %s is in DELETED state for client '
                '%s — creating a replacement',
                client.stripe_customer_id, client.pk)

    customer = stripe.Customer.create(
        email=client.user.email,
        name=client.firm_name,
        metadata={'client_profile_id': str(client.id)},
    )
    client.stripe_customer_id = customer.id
    client.save(update_fields=['stripe_customer_id', 'updated_at'])
    return customer


def _create_build_invoice(client, contract, kind, description):
    """Shared deposit/final invoice builder. Stripe auto-emails the invoice.

    The invoice is created FIRST and the line item attached to it directly
    (``invoice=...``) with ``pending_invoice_items_behavior='exclude'``, so
    (a) the item reliably lands on this invoice instead of staying pending,
    and (b) no unrelated pending item gets swept in. The old order (item
    then bare ``Invoice.create``) produced an empty $0 invoice on current
    Stripe API versions and left the charge pending — which then got swept
    into the next subscription's first invoice.
    """
    _init()
    customer = create_or_get_customer(client)
    amount = contract.deposit_amount if kind == 'deposit' else contract.final_amount
    invoice = stripe.Invoice.create(
        customer=customer.id,
        collection_method='send_invoice',
        days_until_due=7,
        pending_invoice_items_behavior='exclude',
        metadata={
            'kind': kind,
            'client_profile_id': str(client.id),
            'contract_id': str(contract.id),
        },
    )
    stripe.InvoiceItem.create(
        customer=customer.id,
        invoice=invoice.id,
        amount=_cents(amount),
        currency='usd',
        description=description,
    )
    # Finalize (not send_invoice): this makes the invoice payable and gives
    # us hosted_invoice_url, but does NOT trigger Stripe's own email. We send
    # our own branded email with the pay link instead.
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    return invoice


def create_deposit_invoice(client, contract):
    """Create + send the 50% deposit invoice for a build contract."""
    label = contract.get_package_display()
    return _create_build_invoice(
        client, contract, 'deposit', f'{label} — Deposit (50%)',
    )


def create_final_invoice(client, contract):
    """Create + send the final 50% invoice for a build contract."""
    label = contract.get_package_display()
    return _create_build_invoice(
        client, contract, 'final', f'{label} — Final Payment',
    )


def get_maintenance_tier(plan_slug):
    """
    Look up an active maintenance ServiceTier by slug. Raises ValueError
    if the slug is invalid OR the tier has no Stripe Price ID set.

    Public so views can use it for confirm-page render and for early
    validation before hitting Stripe.
    """
    from billing.pricing_models import ServiceTier

    tier = ServiceTier.objects.filter(
        slug=plan_slug, is_active=True, category='maintenance').first()
    if tier is None:
        raise ValueError(f'No active maintenance tier with slug "{plan_slug}".')
    if not tier.stripe_price_id:
        raise ValueError(
            f"No Stripe Price ID set for '{tier.name}'. Run "
            f"`python manage.py sync_stripe_products` to bootstrap it.")
    return tier


# Map a ServiceTier slug to the ClientProfile.PACKAGE_CHOICES code so the
# local `package` column stays in sync with Stripe.
MAINTENANCE_TIER_TO_PACKAGE = {
    'maintenance-essentials': 'maintenance_essentials',
    'maintenance-growth': 'maintenance_growth',
    'maintenance-dominant': 'maintenance_dominant',
}


def create_maintenance_subscription(client, plan_slug):
    """
    Create-or-return a recurring maintenance subscription for the client.

    Idempotent: if the client already has an active/trialing maintenance
    subscription on Stripe, the existing one is returned rather than
    double-billing. If the existing subscription is on a DIFFERENT
    tier from the one requested, callers should use
    `change_maintenance_subscription_tier` instead — this function does
    NOT swap tiers.

    Like `create_hosting_subscription`, we INTENTIONALLY do NOT pass
    `default_payment_method` so renewals fall back to the customer's
    invoice-settings default. Whichever card the client marks Default
    in /portal/subscriptions/ is what Stripe charges.

    Raises:
      StripeNotConfigured — STRIPE_SECRET_KEY missing
      ValueError          — bad plan slug, no Price ID, no PM on file
    """
    _init()
    tier = get_maintenance_tier(plan_slug)
    customer = create_or_get_customer(client)

    # If we already have a maintenance sub on file, return it (or its
    # fresh state) rather than double-creating.
    if client.stripe_subscription_id:
        try:
            existing = stripe.Subscription.retrieve(
                client.stripe_subscription_id)
            existing_status = getattr(existing, 'status', '')
            if existing_status not in ('canceled', 'incomplete_expired'):
                logger.info(
                    'create_maintenance_subscription: client %s already '
                    'has subscription %s (%s) — skipping create',
                    client.pk, existing.id, existing_status)
                return existing
        except Exception:
            client.stripe_subscription_id = ''

    # Confirm the customer has a default payment method on file.
    default_pm = get_customer_default_payment_method(customer.id)
    if not default_pm:
        raise ValueError(
            'No default payment method on file. Add a card on the '
            'subscriptions page before subscribing.')

    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{'price': tier.stripe_price_id}],
        metadata={
            'kind': 'maintenance',
            'client_profile_id': str(client.id),
            'plan': tier.slug,
        },
        # `payment_behavior='error_if_incomplete'` makes the API call
        # fail loud if the saved card declines — better than silently
        # creating an `incomplete` subscription that just sits there.
        payment_behavior='error_if_incomplete',
    )
    client.stripe_subscription_id = subscription.id
    client.package = MAINTENANCE_TIER_TO_PACKAGE.get(tier.slug, client.package)
    client.save(update_fields=[
        'stripe_subscription_id', 'package', 'updated_at',
    ])
    logger.info(
        'create_maintenance_subscription: client %s subscribed to %s '
        '(sub %s)', client.pk, tier.slug, subscription.id)
    return subscription


def _metadata_dict(obj):
    """Coerce a Stripe object's `metadata` to a plain dict.

    Stripe Python v15 StripeObjects are attribute-only — both `**meta`
    (used to merge existing metadata) and `meta.get(...)` raise
    ("'StripeObject' object is not a mapping" / AttributeError). This
    returns a real dict we can safely splat and mutate.
    """
    meta = getattr(obj, 'metadata', None)
    if not meta:
        return {}
    if isinstance(meta, dict):
        return dict(meta)
    for attr in ('to_dict_recursive', 'to_dict'):
        fn = getattr(meta, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:  # noqa: BLE001
                pass
    try:
        return {k: meta[k] for k in meta.keys()}
    except Exception:  # noqa: BLE001
        return {}


def _ts_to_dt(ts):
    """Unix timestamp (Stripe) → aware UTC datetime, or None."""
    if not ts:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


def _change_subscription_tier(sub_id, new_price_id, new_amount_cents,
                              extra_metadata):
    """Swap a subscription to `new_price_id`, choosing billing behaviour
    by direction:

      * UPGRADE  (new price > current) — takes effect immediately and
        invoices the prorated difference NOW (`always_invoice`). Any
        pending downgrade schedule is released first.
      * DOWNGRADE (new price < current) — NO charge now. The lower price
        is queued to take effect at the end of the current paid period
        via a Stripe SubscriptionSchedule; the client keeps the current
        plan until then.
      * SAME price — plain swap, no proration.

    Also un-cancels a sub set to cancel-at-period-end (re-subscribe).

    Returns {'direction': 'upgrade'|'downgrade'|'same',
             'effective_ts': <unix ts when a downgrade applies> | None}.
    """
    sub = stripe.Subscription.retrieve(sub_id)
    items_obj = getattr(sub, 'items', None)
    items_data = list(getattr(items_obj, 'data', [])) if items_obj else []
    if not items_data:
        raise ValueError(f'Subscription {sub_id} has no items.')
    item = items_data[0]
    item_id = item.id
    current_price = getattr(item, 'price', None)
    current_amount = getattr(current_price, 'unit_amount', None)
    current_price_id = getattr(current_price, 'id', None)

    base_meta = _metadata_dict(sub)
    base_meta.update(extra_metadata)

    if (current_amount is not None and new_amount_cents is not None
            and new_amount_cents < current_amount):
        direction = 'downgrade'
    elif (current_amount is not None and new_amount_cents is not None
            and new_amount_cents == current_amount):
        direction = 'same'
    else:
        # Unknown amounts (shouldn't happen) → treat as an upgrade so we
        # never silently defer a change the client expects immediately.
        direction = 'upgrade'

    existing_schedule = getattr(sub, 'schedule', None)

    if direction == 'downgrade':
        # Defer to period end with a subscription schedule. No charge now.
        if existing_schedule:
            sched = stripe.SubscriptionSchedule.retrieve(existing_schedule)
        else:
            sched = stripe.SubscriptionSchedule.create(
                from_subscription=sub_id)
        phases = list(getattr(sched, 'phases', []) or [])
        if not phases:
            raise ValueError(
                f'Could not schedule downgrade for {sub_id}: no phases.')
        cur = phases[0]
        stripe.SubscriptionSchedule.modify(
            sched.id,
            end_behavior='release',
            proration_behavior='none',
            phases=[
                {
                    'items': [{'price': current_price_id, 'quantity': 1}],
                    'start_date': cur.start_date,
                    'end_date': cur.end_date,
                },
                {
                    'items': [{'price': new_price_id, 'quantity': 1}],
                    'iterations': 1,
                },
            ],
        )
        return {'direction': 'downgrade',
                'effective_ts': getattr(cur, 'end_date', None)}

    # upgrade / same — release any pending downgrade schedule first, or
    # Stripe rejects the direct item swap on a schedule-managed sub.
    if existing_schedule:
        try:
            stripe.SubscriptionSchedule.release(existing_schedule)
        except Exception:  # noqa: BLE001
            logger.exception(
                'Could not release schedule %s before tier change',
                existing_schedule)

    stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=False,
        items=[{'id': item_id, 'price': new_price_id}],
        proration_behavior=(
            'always_invoice' if direction == 'upgrade' else 'none'),
        metadata=base_meta,
    )
    return {'direction': direction, 'effective_ts': None}


def change_maintenance_subscription_tier(client, new_plan_slug):
    """
    Swap an existing maintenance subscription to a different tier.

    Upgrades charge the prorated difference immediately; downgrades are
    queued for the end of the current paid period (no charge until then).
    See `_change_subscription_tier`. Returns its result dict so the view
    can word the confirmation accordingly. The subscription ID is kept.

    Raises ValueError if the client has no active subscription to modify
    or the new slug is invalid.
    """
    _init()
    if not client.stripe_subscription_id:
        raise ValueError(
            'No active maintenance subscription to change. Subscribe '
            'first.')

    tier = get_maintenance_tier(new_plan_slug)
    result = _change_subscription_tier(
        client.stripe_subscription_id,
        tier.stripe_price_id,
        _cents(tier.price),
        {'kind': 'maintenance', 'plan': tier.slug},
    )
    _sync_local_maintenance_after_change(client, tier, result)
    logger.info(
        'change_maintenance_subscription_tier: client %s -> %s (%s)',
        client.pk, tier.slug, result['direction'])
    return result


def _sync_local_maintenance_after_change(client, tier, result):
    """Mirror a tier change onto the local MaintenancePlan + package.

    Upgrade/same apply now: flip tier_slug + package and clear any pending
    downgrade. Downgrade is deferred: leave the current tier in place and
    record the queued tier + effective date so the portal can show it.
    """
    from clients.service_models import MaintenancePlan
    sub_id = client.stripe_subscription_id
    plan = MaintenancePlan.objects.filter(
        stripe_subscription_id=sub_id).first()
    if plan is None:
        account = getattr(client, 'migrated_account', None)
        if account is not None:
            plan = account.maintenance_plans.filter(status='active').first()

    if result['direction'] == 'downgrade':
        if plan is not None:
            plan.pending_tier_slug = tier.slug
            plan.pending_tier_effective = _ts_to_dt(result['effective_ts'])
            plan.save(update_fields=[
                'pending_tier_slug', 'pending_tier_effective', 'updated_at'])
        # Package + tier stay on the current (paid) plan until it applies.
        return

    # upgrade / same — effective immediately
    client.package = MAINTENANCE_TIER_TO_PACKAGE.get(tier.slug, client.package)
    client.save(update_fields=['package', 'updated_at'])
    if plan is not None:
        plan.tier_slug = tier.slug
        plan.pending_tier_slug = ''
        plan.pending_tier_effective = None
        plan.save(update_fields=[
            'tier_slug', 'pending_tier_slug', 'pending_tier_effective',
            'updated_at'])


def _maintenance_plan_for(client, website=None):
    """Return the MaintenancePlan holding this client's Stripe sub, or None.

    Mirrors `_social_plan_for`. Null-safe for both an Account and a legacy
    ClientProfile — both expose `.user`, and the Account hangs off it.
    """
    account = getattr(getattr(client, 'user', None), 'account', None)
    if account is None:
        return None
    from clients.service_models import MaintenancePlan
    qs = MaintenancePlan.objects.filter(account=account).exclude(
        stripe_subscription_id='')
    if website is not None:
        qs = qs.filter(website=website)
    plans = list(qs)
    if not plans:
        return None
    # A live plan beats a finished one when a client has re-subscribed.
    rank = {'active': 0, 'awaiting_payment': 1, 'cancelled': 2}
    plans.sort(key=lambda p: (rank.get(p.status, 3), -p.created_at.timestamp()))
    return plans[0]


def _maintenance_sub_id(client, website=None):
    """(subscription_id, plan) for this client's maintenance subscription.

    The id lives on MaintenancePlan post-refactor; `Account` has no
    `stripe_subscription_id` column at all, so the getattr fallback is what
    keeps a legacy ClientProfile working without raising on an Account.
    """
    plan = _maintenance_plan_for(client, website)
    sub_id = ((plan.stripe_subscription_id if plan else '')
              or getattr(client, 'stripe_subscription_id', '') or '')
    return sub_id, plan


def _forget_dead_maintenance_sub(client, plan, sub_id):
    """Drop a subscription id Stripe no longer recognises."""
    if plan is not None and plan.stripe_subscription_id == sub_id:
        plan.stripe_subscription_id = ''
        plan.status = 'ended'
        plan.save(update_fields=[
            'stripe_subscription_id', 'status', 'updated_at'])
    if getattr(client, 'stripe_subscription_id', '') == sub_id:
        client.stripe_subscription_id = ''
        fields = ['stripe_subscription_id', 'updated_at']
        if hasattr(client, 'maintenance_active'):
            client.maintenance_active = False
            fields.insert(1, 'maintenance_active')
        client.save(update_fields=fields)


def cancel_maintenance_subscription(client, reason='', website=None):
    """
    Cancel the client's maintenance subscription at period end so they
    keep service through what they've already paid for. Local
    `maintenance_active` flips False only when the period actually
    ends (handled in `customer.subscription.deleted` webhook).

    `client` is the Account, which has no `stripe_subscription_id` column —
    reading it directly raised AttributeError, which the portal view
    swallowed into "Could not cancel". The button therefore never worked
    for any account-based client. Resolve through MaintenancePlan instead.
    """
    _init()
    sub_id, plan = _maintenance_sub_id(client, website)
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        _forget_dead_maintenance_sub(client, plan, sub_id)
        return None
    if getattr(sub, 'status', '') in ('canceled', 'incomplete_expired'):
        return sub
    _cancel_meta = _metadata_dict(sub)
    _cancel_meta['cancel_reason'] = reason[:200] if reason else ''
    updated = stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=True,
        metadata=_cancel_meta,
    )
    if plan is not None and plan.status in ('active', 'awaiting_payment'):
        plan.status = 'cancelled'
        plan.cancelled_at = timezone.now()
        plan.save(update_fields=['status', 'cancelled_at', 'updated_at'])
    logger.info(
        'cancel_maintenance_subscription: client %s sub %s '
        'cancel_at_period_end=True (reason=%s)',
        client.pk, sub_id, reason)
    return updated


def resume_maintenance_subscription(client, website=None):
    """
    Undo a pending cancel-at-period-end on the maintenance sub. No-op
    if the sub already isn't scheduled to cancel. Resolves the
    subscription the same way `cancel_maintenance_subscription` does.
    """
    _init()
    sub_id, plan = _maintenance_sub_id(client, website)
    if not sub_id:
        return None
    updated = stripe.Subscription.modify(
        sub_id, cancel_at_period_end=False,
    )
    if plan is not None and plan.status == 'cancelled':
        plan.status = 'active'
        plan.cancelled_at = None
        plan.save(update_fields=['status', 'cancelled_at', 'updated_at'])
    return updated


# ── Social media subscriptions ─────────────────────────────────────────────


def get_social_tier(plan_slug):
    """Look up an active social_media ServiceTier by slug. Same shape as
    get_maintenance_tier — raises ValueError on bad slug / missing
    Stripe Price ID."""
    from billing.pricing_models import ServiceTier

    tier = ServiceTier.objects.filter(
        slug=plan_slug, is_active=True, category='social_media',
    ).first()
    if tier is None:
        raise ValueError(
            f'No active social media tier with slug "{plan_slug}".')
    if not tier.stripe_price_id:
        raise ValueError(
            f"No Stripe Price ID set for '{tier.name}'. Run "
            f"`python manage.py sync_stripe_products` to bootstrap it.")
    return tier


def _ensure_social_plan(client, tier, website=None):
    """Idempotently upsert a SocialMediaPlan keyed on (account, website)
    so the Social Media manager (filters status='active') picks up the
    client after signup. Per-Website: one plan per business.

    `website` should be a Website instance scoped to the same Account.
    Pass None for legacy account-wide signups (matches the row created
    when there are no Websites yet)."""
    account = getattr(getattr(client, 'user', None), 'account', None)
    if account is None:
        return None
    from clients.service_models import SocialMediaPlan
    plan = SocialMediaPlan.objects.filter(
        account=account, website=website,
    ).first()
    max_channels = tier.max_channels or 2
    if plan is None:
        plan = SocialMediaPlan.objects.create(
            account=account,
            website=website,
            tier_slug=tier.slug,
            status='active',
            stripe_subscription_id=client.stripe_social_subscription_id or '',
            max_channels=max_channels,
        )
    else:
        plan.tier_slug = tier.slug
        plan.status = 'active'
        plan.stripe_subscription_id = (
            client.stripe_social_subscription_id or '')
        plan.max_channels = max_channels
        plan.save(update_fields=[
            'tier_slug', 'status', 'stripe_subscription_id',
            'max_channels', 'updated_at',
        ])
    return plan


def _social_plan_for(client, website):
    """Return the existing SocialMediaPlan row for (account, website),
    or None. Caller-friendly null-safe."""
    account = getattr(getattr(client, 'user', None), 'account', None)
    if account is None:
        return None
    from clients.service_models import SocialMediaPlan
    return SocialMediaPlan.objects.filter(
        account=account, website=website,
    ).first()


def create_social_subscription(client, plan_slug, website=None):
    """Create-or-return a recurring social subscription for ONE Website
    on this client's Account. Stripe sub id is stored per-Website on
    SocialMediaPlan.stripe_subscription_id. ClientProfile.stripe_social
    _subscription_id mirrors the first sub created (so the legacy
    "primary" pointer keeps working for /portal/subscriptions)."""
    _init()
    tier = get_social_tier(plan_slug)
    customer = create_or_get_customer(client)

    # Idempotency: if a SocialMediaPlan already exists for this Website
    # with a live Stripe sub, return that sub instead of double-billing.
    existing_plan = _social_plan_for(client, website)
    if existing_plan and existing_plan.stripe_subscription_id:
        try:
            existing = stripe.Subscription.retrieve(
                existing_plan.stripe_subscription_id)
            if getattr(existing, 'status', '') not in (
                    'canceled', 'incomplete_expired'):
                logger.info(
                    'create_social_subscription: client %s website %s '
                    'already has sub %s — skipping create',
                    client.pk, getattr(website, 'pk', None), existing.id)
                _ensure_social_plan(client, tier, website=website)
                return existing
        except Exception:
            existing_plan.stripe_subscription_id = ''
            existing_plan.save(update_fields=[
                'stripe_subscription_id', 'updated_at'])

    default_pm = get_customer_default_payment_method(customer.id)
    if not default_pm:
        raise ValueError(
            'No default payment method on file. Add a card on the '
            'subscriptions page before subscribing.')

    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{'price': tier.stripe_price_id}],
        metadata={
            'kind': 'social_media',
            'client_profile_id': str(client.id),
            'website_id': str(getattr(website, 'id', '') or ''),
            'plan': tier.slug,
        },
        payment_behavior='error_if_incomplete',
    )

    # Mirror the new sub id onto CP only when it's the first one — so
    # the legacy "primary" pointer doesn't get overwritten by every
    # subsequent per-Website signup.
    if not client.stripe_social_subscription_id:
        client.stripe_social_subscription_id = subscription.id
        client.save(update_fields=[
            'stripe_social_subscription_id', 'updated_at',
        ])

    plan = _ensure_social_plan(client, tier, website=website)
    if plan is not None:
        plan.stripe_subscription_id = subscription.id
        plan.save(update_fields=[
            'stripe_subscription_id', 'updated_at'])

    logger.info(
        'create_social_subscription: client %s website %s subscribed '
        'to %s (sub %s)',
        client.pk, getattr(website, 'pk', None), tier.slug,
        subscription.id)
    return subscription


def change_social_subscription_tier(client, new_plan_slug, website=None):
    """Swap an existing per-Website social sub to a different tier.

    Upgrades charge the prorated difference immediately; downgrades are
    queued for the end of the current paid period (no charge until then).
    Sub id stays the same. Returns the `_change_subscription_tier` result
    dict so the view can word the confirmation accordingly."""
    _init()
    plan = _social_plan_for(client, website)
    sub_id = (plan.stripe_subscription_id if plan else '') \
        or client.stripe_social_subscription_id
    if not sub_id:
        raise ValueError(
            'No active social media subscription to change. '
            'Subscribe first.')

    tier = get_social_tier(new_plan_slug)
    result = _change_subscription_tier(
        sub_id,
        tier.stripe_price_id,
        _cents(tier.price),
        {'kind': 'social_media', 'plan': tier.slug},
    )

    if result['direction'] == 'downgrade':
        # Keep the current paid tier; record the queued downgrade.
        if plan is not None:
            plan.pending_tier_slug = tier.slug
            plan.pending_tier_effective = _ts_to_dt(result['effective_ts'])
            plan.save(update_fields=[
                'pending_tier_slug', 'pending_tier_effective', 'updated_at'])
    else:
        # upgrade / same — apply now (also clears any pending downgrade).
        plan = _ensure_social_plan(client, tier, website=website)
        if plan is not None:
            fields = ['pending_tier_slug', 'pending_tier_effective',
                      'updated_at']
            plan.pending_tier_slug = ''
            plan.pending_tier_effective = None
            if not plan.stripe_subscription_id:
                plan.stripe_subscription_id = sub_id
                fields.append('stripe_subscription_id')
            plan.save(update_fields=fields)

    logger.info(
        'change_social_subscription_tier: client %s website %s -> %s (%s)',
        client.pk, getattr(website, 'pk', None), tier.slug,
        result['direction'])
    return result


def cancel_social_subscription(client, reason='', website=None):
    """Cancel a per-Website social sub at period end."""
    _init()
    plan = _social_plan_for(client, website)
    sub_id = (plan.stripe_subscription_id if plan else '') \
        or client.stripe_social_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        if plan and plan.stripe_subscription_id == sub_id:
            plan.stripe_subscription_id = ''
            plan.save(update_fields=[
                'stripe_subscription_id', 'updated_at'])
        if client.stripe_social_subscription_id == sub_id:
            client.stripe_social_subscription_id = ''
            client.save(update_fields=[
                'stripe_social_subscription_id', 'updated_at'])
        return None
    if getattr(sub, 'status', '') in ('canceled', 'incomplete_expired'):
        return sub
    _cancel_meta = _metadata_dict(sub)
    _cancel_meta['cancel_reason'] = reason[:200] if reason else ''
    return stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=True,
        metadata=_cancel_meta,
    )


def resume_social_subscription(client, website=None):
    """Undo a pending cancel-at-period-end on a per-Website social sub."""
    _init()
    plan = _social_plan_for(client, website)
    sub_id = (plan.stripe_subscription_id if plan else '') \
        or client.stripe_social_subscription_id
    if not sub_id:
        return None
    return stripe.Subscription.modify(
        sub_id, cancel_at_period_end=False,
    )


def get_hosting_price_id():
    """
    Returns the Stripe Price ID for the annual hosting subscription.

    Pulled from settings.STRIPE_PRICE_HOSTING_YEARLY (env var). The
    `sync_stripe_subscription_products` management command bootstraps
    the Stripe Product + Price and prints the ID for the operator to
    paste into .env.
    """
    pid = getattr(settings, 'STRIPE_PRICE_HOSTING_YEARLY', '')
    if not pid:
        raise StripeNotConfigured(
            'STRIPE_PRICE_HOSTING_YEARLY is not set in .env — run '
            '`python manage.py sync_stripe_subscription_products` '
            'to bootstrap the hosting Product + Price, then paste '
            'the printed Price ID into .env.')
    return pid


def attach_payment_method_to_customer(customer_id, payment_method_id,
                                      set_as_default=True):
    """
    Attach a payment method to a Stripe Customer, optionally setting
    it as the default invoice payment method. Idempotent — Stripe
    is fine with re-attaching the same PM.
    """
    _init()
    try:
        stripe.PaymentMethod.attach(
            payment_method_id, customer=customer_id)
    except stripe.error.InvalidRequestError as exc:
        # "already attached to this customer" is OK; anything else
        # is a real error.
        if 'already' not in str(exc).lower():
            raise
    if set_as_default:
        stripe.Customer.modify(
            customer_id,
            invoice_settings={
                'default_payment_method': payment_method_id,
            },
        )


def create_hosting_subscription(client, default_payment_method_id=None):
    """
    Create the annual hosting subscription for a client.

    `trial_period_days=365` so the FIRST recurring charge fires 365
    days from now — the lump-sum payment they just made covers year 1.
    Subsequent renewals are gated by the `invoice.upcoming` webhook in
    billing/webhooks.py.

    We INTENTIONALLY do NOT set `default_payment_method` on the
    subscription itself. Stripe's charge priority is:
      1. subscription.default_payment_method
      2. customer.invoice_settings.default_payment_method
    Skipping #1 means every renewal falls back to the customer
    default, so whichever card the client marks as Default in
    /portal/subscriptions/ is the one Stripe charges. The
    `default_payment_method_id` arg is still accepted for backward
    compatibility but it's IGNORED — passing it would lock the
    subscription to that one card forever.

    Returns the Stripe Subscription object. Idempotent: if the client
    already has a hosting subscription, returns the existing one
    rather than double-billing.
    """
    _init()
    if client.stripe_hosting_subscription_id:
        try:
            existing = stripe.Subscription.retrieve(
                client.stripe_hosting_subscription_id)
            existing_status = getattr(existing, 'status', '')
            if existing_status not in ('canceled', 'incomplete_expired'):
                logger.info(
                    'create_hosting_subscription: client %s already has '
                    'subscription %s — skipping', client.pk, existing.id)
                return existing
        except Exception:
            # Subscription doesn't exist any more on Stripe's side —
            # fall through and create a fresh one.
            client.stripe_hosting_subscription_id = ''

    if not client.stripe_customer_id:
        raise ValueError(
            f'Client {client.pk} has no stripe_customer_id — cannot '
            f'create a subscription.')

    # default_payment_method_id is intentionally NOT passed to
    # stripe.Subscription.create — see docstring. We do still want
    # the customer to HAVE a default though, so the very first
    # renewal doesn't fail; the webhook that calls us already
    # called attach_payment_method_to_customer(set_as_default=True)
    # before reaching here.
    sub = stripe.Subscription.create(
        customer=client.stripe_customer_id,
        items=[{'price': get_hosting_price_id()}],
        trial_period_days=365,
        metadata={
            'kind': 'hosting',
            'client_profile_id': str(client.id),
        },
        payment_behavior='allow_incomplete',
    )
    client.stripe_hosting_subscription_id = sub.id
    client.save(update_fields=[
        'stripe_hosting_subscription_id', 'updated_at'])
    logger.info(
        'create_hosting_subscription: client %s subscribed (sub %s, '
        'trial until %s — renewals will charge customer.default_pm)',
        client.pk, sub.id, getattr(sub, 'trial_end', None))
    return sub


def cancel_hosting_subscription(client, reason=''):
    """
    Cancel the client's hosting subscription at the end of the
    current period (so they don't lose access mid-cycle). Sets
    cancel_at_period_end=True; the row stays on Stripe for history
    but won't generate any future invoices.

    No-op if the client has no hosting sub or it's already canceled.
    """
    _init()
    sub_id = client.stripe_hosting_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        # Already gone from Stripe — clear our reference.
        client.stripe_hosting_subscription_id = ''
        client.save(update_fields=[
            'stripe_hosting_subscription_id', 'updated_at'])
        return None
    if getattr(sub, 'status', '') in ('canceled', 'incomplete_expired'):
        return sub
    _cancel_meta = _metadata_dict(sub)
    _cancel_meta['cancel_reason'] = reason[:200] if reason else ''
    updated = stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=True,
        metadata=_cancel_meta,
    )
    logger.info(
        'cancel_hosting_subscription: client %s sub %s set to '
        'cancel_at_period_end (reason=%s)',
        client.pk, sub_id, reason)
    return updated


def list_customer_payment_methods(customer_id):
    """List active card payment methods attached to a customer.

    Returns plain dicts (via .to_dict_recursive()) so the template can
    iterate them as `pm.card.last4` etc. via dot-attribute, and Django
    template lookups don't trip on StripeObject's restrictive
    attribute-only API (no .get())."""
    _init()
    if not customer_id:
        return []
    methods = stripe.PaymentMethod.list(
        customer=customer_id, type='card', limit=20)
    return list(methods.data) if hasattr(methods, 'data') else []


def get_customer_default_payment_method(customer_id):
    """Return the customer's default invoice payment method ID, or ''.

    Self-heals a stale default: repeated checkouts / manual add+remove
    can leave invoice_settings.default_payment_method pointing at a card
    that's no longer attached, which made "card on file" checks (e.g.
    the social / maintenance subscribe pages) report no card even though
    one was attached. If the stored default isn't among the currently
    attached cards, fall back to the first attached card and persist it
    as the new default so renewals don't fail either.
    """
    _init()
    if not customer_id:
        return ''
    cust = stripe.Customer.retrieve(customer_id)
    # Stripe v8+ removed dict-like .get() on StripeObject — attr access
    # only. invoice_settings may be None if never set.
    inv = getattr(cust, 'invoice_settings', None)
    stored = getattr(inv, 'default_payment_method', '') if inv else ''

    cards = list_customer_payment_methods(customer_id)
    card_ids = [getattr(m, 'id', '') for m in cards]
    if stored and stored in card_ids:
        return stored
    if card_ids:
        first = card_ids[0]
        # Heal the pointer so the rest of the system (renewals, badges)
        # agrees there's a usable default.
        try:
            set_customer_default_payment_method(customer_id, first)
        except Exception:  # noqa: BLE001
            logger.exception(
                'could not heal default PM for %s', customer_id)
        return first
    return ''


def set_customer_default_payment_method(customer_id, payment_method_id):
    """Set the default invoice payment method on a customer."""
    _init()
    stripe.Customer.modify(
        customer_id,
        invoice_settings={
            'default_payment_method': payment_method_id,
        },
    )


def detach_payment_method(payment_method_id):
    """Detach (remove) a payment method from its customer."""
    _init()
    return stripe.PaymentMethod.detach(payment_method_id)


def create_setup_intent_for_customer(customer_id):
    """
    Create a Stripe SetupIntent so a client can save a new card via
    Stripe Elements on the portal without making a payment. Returns
    the SetupIntent object — the caller hands its `client_secret`
    to Stripe.js.
    """
    _init()
    intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=['card'],
        usage='off_session',
    )
    return intent


def create_onboarding_payment_intent(*, email, name, line_items,
                                     client_profile_id, invoice_id):
    """
    Create-or-reuse a Stripe Customer + a single PaymentIntent for the
    new on-site onboarding payment flow.

    Replaces `create_onboarding_invoice` (which used Stripe Invoices +
    Stripe-hosted hosted-invoice pages). We DON'T create a Stripe
    Invoice — the line items live on our OnboardingInvoice row and
    render on our own payment page. Stripe just processes the card.

    Settings:
      - `payment_method_types=['card']` — card-only. No Apple Pay /
        Google Pay / Link / Affirm / etc. (Per spec — wallets are
        explicitly off.)
      - `receipt_email` is intentionally NOT set — Stripe only sends
        its built-in receipt when this is provided. We send our own
        branded PDF receipt instead.
      - `metadata.invoice_id` lets the webhook find the OnboardingInvoice
        on `payment_intent.succeeded`.

    Returns (customer, payment_intent).
    """
    _init()
    customer = stripe.Customer.create(
        email=email,
        name=name,
        metadata={
            'source': 'aspired_websites',
            'client_profile_id': str(client_profile_id),
        },
    )

    total = sum(item['amount'] for item in line_items)
    description_lines = ' · '.join(item['description'] for item in line_items)

    payment_intent = stripe.PaymentIntent.create(
        amount=_cents(total),
        currency='usd',
        customer=customer.id,
        # Card-only — wallets explicitly off per spec.
        payment_method_types=['card'],
        # No `receipt_email` => no Stripe receipt; we send our own.
        description=f'Aspired Websites — {description_lines}'[:1000],
        # Save the card off-session so the same card auto-renews the
        # hosting subscription (and any future subs) without asking
        # again. The webhook attaches it to the customer + sets as
        # default after the PI succeeds.
        setup_future_usage='off_session',
        metadata={
            'source': 'aspired_websites',
            'kind': 'onboarding',
            'client_profile_id': str(client_profile_id),
            'invoice_id': str(invoice_id),
        },
    )
    return customer, payment_intent


def create_onboarding_invoice(*, email, name, line_items, client_profile_id):
    """
    Create + finalize a single one-off Stripe invoice for the new admin
    onboarding-invoice flow (Part 2 of the onboarding build).

    `line_items` is a list of {'description': str, 'amount': Decimal} dicts.
    Returns (customer, invoice) where invoice is already finalized — Stripe
    automatically emails the hosted invoice link to the customer.

    Metadata kind='onboarding_setup' is set on the invoice so the webhook
    handler can distinguish this from contract-flow deposit/final invoices.
    """
    _init()
    customer = stripe.Customer.create(
        email=email,
        name=name,
        metadata={
            'source': 'aspired_websites',
            'client_profile_id': str(client_profile_id),
        },
    )
    for item in line_items:
        stripe.InvoiceItem.create(
            customer=customer.id,
            amount=_cents(item['amount']),
            currency='usd',
            description=item['description'],
        )
    invoice = stripe.Invoice.create(
        customer=customer.id,
        collection_method='send_invoice',
        days_until_due=7,
        auto_advance=True,
        metadata={
            'kind': 'onboarding_setup',
            'client_profile_id': str(client_profile_id),
        },
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    # finalize_invoice with collection_method='send_invoice' auto-emails
    # the hosted invoice link — no separate send_invoice() call required.
    return customer, invoice


def get_domain_tier(tld):
    """
    Return the ServiceTier that prices a domain in `tld`.

    Premium TLDs (currently just .law) use the 'domain-law' tier;
    everything else uses 'domain-standard'. The mapping lives in
    domains.models.tier_slug_for_tld so adding new premium TLDs is
    a one-line change there.
    """
    from billing.pricing_models import ServiceTier
    from domains.models import tier_slug_for_tld

    slug = tier_slug_for_tld(tld.lower().lstrip('.'))
    tier = ServiceTier.objects.filter(
        slug=slug, is_active=True, category='addon').first()
    if tier is None:
        raise ValueError(
            f'No active domain pricing tier "{slug}" — run seed_pricing.')
    if not tier.stripe_price_id:
        raise ValueError(
            f'Domain tier "{slug}" has no Stripe Price ID — run '
            f'`python manage.py sync_stripe_products`.')
    return tier


def create_domain_subscription(client, registration):
    """
    Create a Stripe Subscription for a domain registration.

    The subscription:
      - charges $75 (or $175 for .law) IMMEDIATELY on the customer's
        default card (no trial — they're paying for year 1 right now)
      - auto-renews in 365 days
      - is gated by invoice.upcoming (we cancel if the domain has
        been transferred out or the client cancelled their plan
        between renewals)

    The webhook for the initial invoice.paid is what marks the
    DomainRegistration row as billed; this function only kicks off
    the charge.

    Returns the Stripe Subscription object. Raises ValueError if the
    customer has no default payment method on file (the portal flow
    forces an add-card step before calling this).
    """
    _init()
    tier = get_domain_tier(registration.tld)
    customer = create_or_get_customer(client)

    default_pm = get_customer_default_payment_method(customer.id)
    if not default_pm:
        raise ValueError(
            'No default payment method on file. Add a card on the '
            'subscriptions page before registering a domain.')

    sub = stripe.Subscription.create(
        customer=customer.id,
        items=[{'price': tier.stripe_price_id}],
        metadata={
            'kind': 'domain',
            'client_profile_id': str(client.id),
            'domain_registration_id': str(registration.id),
            'domain_name': registration.domain_name,
            'tld': registration.tld,
        },
        # Stop the API call if the saved card declines — better than
        # an `incomplete` sub that creates a paid-zero placeholder.
        payment_behavior='error_if_incomplete',
    )
    registration.stripe_subscription_id = sub.id
    registration.pricing_tier_slug = tier.slug
    registration.save(update_fields=[
        'stripe_subscription_id', 'pricing_tier_slug', 'updated_at'])
    logger.info(
        'create_domain_subscription: client %s domain %s sub %s',
        client.pk, registration.domain_name, sub.id)
    return sub


def cancel_domain_subscription(registration, reason=''):
    """
    Cancel a domain Stripe Subscription at period end so the client
    keeps the domain through what they've already paid for. They get
    a transfer-out email immediately so they can move it elsewhere
    before the grace period ends.

    Returns the updated Stripe Subscription object or None if no sub
    on file.
    """
    _init()
    sub_id = registration.stripe_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        registration.stripe_subscription_id = ''
        registration.save(update_fields=[
            'stripe_subscription_id', 'updated_at'])
        return None
    if getattr(sub, 'status', '') in ('canceled', 'incomplete_expired'):
        return sub
    _cancel_meta = _metadata_dict(sub)
    _cancel_meta['cancel_reason'] = reason[:200] if reason else ''
    updated = stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=True,
        metadata=_cancel_meta,
    )
    logger.info(
        'cancel_domain_subscription: domain %s sub %s '
        'cancel_at_period_end=True', registration.domain_name, sub_id)
    return updated


def resume_domain_subscription(registration):
    """
    Undo a pending cancel-at-period-end on a domain Stripe sub. Used
    by `resume_domain` when a client (or admin) changes their mind
    after starting the transfer-out flow.

    No-op if no sub on file (sandbox registrations) or if the sub
    isn't currently scheduled to cancel.
    """
    _init()
    sub_id = registration.stripe_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        registration.stripe_subscription_id = ''
        registration.save(update_fields=[
            'stripe_subscription_id', 'updated_at'])
        return None
    if not getattr(sub, 'cancel_at_period_end', False):
        return sub                          # already not scheduled to cancel
    updated = stripe.Subscription.modify(
        sub_id, cancel_at_period_end=False)
    logger.info(
        'resume_domain_subscription: client %s domain %s '
        'cancel_at_period_end reset', registration.client_id,
        registration.domain_name)
    return updated


def refund_failed_domain_registration(stripe_subscription_id, reason=''):
    """
    Best-effort cleanup when a Stripe charge succeeded but the
    follow-up Namecheap registration FAILED. Cancels the
    subscription + refunds the most recent charge so the client
    isn't out money for a domain we couldn't register.

    Returns True on a successful refund, False otherwise (caller
    logs + alerts admin so it can be handled manually).
    """
    _init()
    if not stripe_subscription_id:
        return False
    try:
        # Cancel immediately so no future invoices generate.
        stripe.Subscription.cancel(
            stripe_subscription_id, invoice_now=False, prorate=False)
    except Exception:
        logger.exception(
            'refund_failed_domain_registration: subscription cancel '
            'failed for %s', stripe_subscription_id)

    # Find the most recent invoice on the sub and refund its PI.
    try:
        invs = stripe.Invoice.list(
            subscription=stripe_subscription_id, limit=1)
        invs_data = list(getattr(invs, 'data', None) or [])
        if not invs_data:
            return False
        invoice = invs_data[0]
        pi = getattr(invoice, 'payment_intent', None)
        if not pi:
            return False
        stripe.Refund.create(
            payment_intent=pi,
            reason='duplicate' if not reason else 'requested_by_customer',
            metadata={'note': reason[:200] if reason else ''},
        )
        return True
    except Exception:
        logger.exception(
            'refund_failed_domain_registration: refund failed for %s',
            stripe_subscription_id)
        return False


def issue_deposit_invoice(contract):
    """
    Best-effort deposit invoice send, called right after a contract is signed.
    Logs and returns None if Stripe is unconfigured — never breaks signing.
    """
    try:
        return create_deposit_invoice(contract.client, contract)
    except StripeNotConfigured:
        logger.warning(
            'Stripe not configured — deposit invoice for contract %s not sent.',
            contract.pk,
        )
    except Exception:
        logger.exception(
            'Failed to issue deposit invoice for contract %s', contract.pk,
        )
    return None


def start_contract_payment(contract, amount, *, is_deposit):
    """
    Set up the inline payment for a just-signed build contract and return the
    OnboardingInvoice the client should be redirected to (``/pay/<token>/``).

    Mirrors the admin onboarding-invoice flow so the build deposit (or
    pay-in-full) is collected on our own Stripe Elements page and chains
    straight into account setup — no separate Stripe-hosted invoice email.

    Creates (or reuses an unpaid) OnboardingInvoice + a card PaymentIntent,
    ensures the account-setup token exists, and flips the client to
    ``pending_setup`` so the post-payment setup link works.

    Returns the OnboardingInvoice, or None if Stripe isn't configured.
    """
    from decimal import Decimal

    from django.utils import timezone

    from clients.models import OnboardingInvoice, OnboardingToken

    # `contract.client` is the legacy ClientProfile FK and is null on every
    # contract raised from the Website/Account pages. Reading it here meant
    # payment could not be started at all for those: the view caught the
    # AttributeError, got None back, and bounced the client to the generic
    # "contract signed" page with no way to pay.
    client = contract.client
    website = contract.website_new
    account = contract.account
    owner = client or account
    if owner is None:
        logger.error(
            'start_contract_payment: contract %s has neither client nor '
            'account — cannot bill anyone.', contract.pk)
        return None

    amount = Decimal(amount)
    label = contract.get_package_display() or 'Website Build'
    desc = f'{label} — {"Deposit (50%)" if is_deposit else "Paid in full"}'

    # Find this build's existing unpaid invoice. Keyed on `client` alone,
    # a null client matched ANY invoice whose legacy FK was unset — i.e.
    # another account's row — and then overwrote it below.
    if client is not None:
        existing = OnboardingInvoice.objects.filter(client=client)
    elif website is not None:
        existing = OnboardingInvoice.objects.filter(website_new=website)
    else:
        existing = OnboardingInvoice.objects.filter(account_new=account)
    invoice = existing.order_by('-created_at').first()
    if invoice is not None and invoice.status == 'paid':
        # Already paid — don't re-charge; just hand it back.
        return invoice

    line_items = [{'description': desc, 'amount': f'{amount:.2f}'}]
    if invoice is None:
        invoice = OnboardingInvoice.objects.create(
            client=client, contract=contract, is_deposit=is_deposit,
            account_new=account, website_new=website,
            line_items=line_items, total_amount=amount, status='draft',
        )
    else:
        invoice.contract = contract
        invoice.is_deposit = is_deposit
        invoice.account_new = account
        invoice.website_new = website
        invoice.line_items = line_items
        invoice.total_amount = amount
        invoice.status = 'draft'
        invoice.save(update_fields=[
            'contract', 'is_deposit', 'account_new', 'website_new',
            'line_items', 'total_amount', 'status', 'updated_at'])

    # Owner-agnostic: Account calls it `name`, ClientProfile `firm_name`.
    owner_email = (owner.user.email if getattr(owner, 'user_id', None)
                   else '') or ''
    owner_name = (getattr(owner, 'firm_name', '')
                  or getattr(owner, 'name', '') or '')

    try:
        customer, payment_intent = create_onboarding_payment_intent(
            email=owner_email,
            name=owner_name,
            line_items=[{'description': desc, 'amount': amount}],
            client_profile_id=owner.id,
            invoice_id=invoice.id,
        )
    except StripeNotConfigured:
        logger.warning(
            'Stripe not configured — contract %s payment not started.',
            contract.pk)
        return None

    owner.stripe_customer_id = customer.id
    owner.save(update_fields=['stripe_customer_id', 'updated_at'])

    invoice.stripe_payment_intent_id = payment_intent.id
    invoice.stripe_client_secret = payment_intent.client_secret
    invoice.status = 'sent'
    invoice.sent_at = timezone.now()
    invoice.save(update_fields=[
        'stripe_payment_intent_id', 'stripe_client_secret',
        'status', 'sent_at', 'updated_at'])

    # Account-setup token for the post-payment /onboarding/setup/ step.
    # The token is account-level (account_new); keying it on a null
    # `client` would have collided every account-based contract onto one
    # row, since OneToOne treats them all as the same null.
    if client is not None:
        OnboardingToken.objects.get_or_create(client=client)
    elif account is not None:
        OnboardingToken.objects.get_or_create(account_new=account)

    if owner.onboarding_status not in (
            'pending_intake', 'onboarding_complete'):
        owner.onboarding_status = 'pending_setup'
        owner.save(update_fields=['onboarding_status', 'updated_at'])

    return invoice


def start_contract_final_payment(contract):
    """Set up the remaining-balance (final 50%) payment for a build on OUR
    own ``/pay/<token>/`` Stripe Elements page — never a Stripe-hosted page.

    Reuses the client's OnboardingInvoice row + Stripe customer (the card
    saved at the deposit) so the whole flow stays on our domain. Returns the
    OnboardingInvoice (call ``.get_pay_url()`` for the on-site link), or None
    when nothing is owed / Stripe isn't configured.
    """
    from decimal import Decimal

    from django.utils import timezone

    from clients.models import OnboardingInvoice

    # Same legacy-FK trap as start_contract_payment: `contract.client` is
    # null on Website/Account-raised contracts, so every read below raised
    # AttributeError. _issue_website_final_invoice swallows it, so moving
    # a build to Pre-Launch quietly raised NO final invoice and sent NO
    # email — while the launch gate went on blocking for a payment the
    # client was never asked to make.
    client = contract.client
    website = contract.website_new
    account = contract.account
    owner = client or account
    if owner is None:
        logger.error(
            'start_contract_final_payment: contract %s has neither client '
            'nor account — cannot bill anyone.', contract.pk)
        return None

    amount = Decimal(contract.final_amount or 0)
    if amount <= 0:
        return None
    label = contract.get_package_display() or 'Website Build'
    desc = f'{label} — Final Payment'

    try:
        _init()
    except StripeNotConfigured:
        return None

    # Reuse the customer that holds the saved card from the deposit.
    customer_id = owner.stripe_customer_id
    if not customer_id:
        try:
            cust = stripe.Customer.create(
                email=((owner.user.email
                        if getattr(owner, 'user_id', None) else '') or ''),
                name=(getattr(owner, 'firm_name', '')
                      or getattr(owner, 'name', '') or ''))
            customer_id = cust.id
            owner.stripe_customer_id = customer_id
            owner.save(update_fields=['stripe_customer_id', 'updated_at'])
        except Exception:
            logger.exception(
                'final payment: customer create failed for %s', owner.pk)
            return None

    line_items = [{'description': desc, 'amount': f'{amount:.2f}'}]

    # The final balance gets its OWN invoice row. This used to reuse
    # whatever invoice the client already had — which was the paid
    # deposit — rewriting its amount, description and status and
    # clearing paid_at. The money was still recorded in PaymentRecord,
    # but the invoice list showed a single row that mutated instead of
    # a deposit and a final, so a client could not see what they had
    # already paid. Only ever reuse an unpaid FINAL row (a re-trigger
    # of Pre-Launch), never the deposit.
    invoice = (OnboardingInvoice.objects
               .filter(contract=contract, is_deposit=False)
               .exclude(status='paid')
               .order_by('-created_at')
               .first())
    if invoice is None:
        invoice = OnboardingInvoice.objects.create(
            client=client, contract=contract, is_deposit=False,
            account_new=contract.account, website_new=contract.website_new,
            line_items=line_items, total_amount=amount, status='draft')
    else:
        invoice.account_new = contract.account
        invoice.website_new = contract.website_new
        invoice.line_items = line_items
        invoice.total_amount = amount
        invoice.status = 'draft'
        invoice.paid_at = None
        invoice.save(update_fields=[
            'account_new', 'website_new', 'line_items', 'total_amount',
            'status', 'paid_at', 'updated_at'])

    try:
        pi = stripe.PaymentIntent.create(
            amount=_cents(amount), currency='usd', customer=customer_id,
            payment_method_types=['card'], setup_future_usage='off_session',
            description=f'Aspired Websites — {desc}'[:1000],
            metadata={
                'source': 'aspired_websites', 'kind': 'onboarding',
                'client_profile_id': str(owner.id),
                'invoice_id': str(invoice.id)})
    except Exception:
        logger.exception(
            'final payment: PaymentIntent create failed for %s', owner.pk)
        return None

    invoice.stripe_payment_intent_id = pi.id
    invoice.stripe_client_secret = pi.client_secret
    invoice.status = 'sent'
    invoice.sent_at = timezone.now()
    invoice.save(update_fields=[
        'stripe_payment_intent_id', 'stripe_client_secret',
        'status', 'sent_at', 'updated_at'])
    return invoice


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.3 — MiniInvoice (out-of-scope work) → Stripe
# ─────────────────────────────────────────────────────────────────────────────

def send_mini_invoice(mini):
    """Create + send a Stripe invoice for an out-of-scope MiniInvoice.

    Refuses to send if amount <= 0 (un-priced); the admin must set
    the dollar amount before pressing Send. Stripe handles the email,
    we just store the invoice id and flip status to 'sent'.

    On `invoice.paid` webhook, billing/webhooks.py picks up the
    `kind=mini_invoice` metadata and flips the row to 'paid'.

    Raises StripeNotConfigured if STRIPE_SECRET_KEY missing.
    Raises ValueError if amount is not a payable number.
    """
    if not mini.amount or Decimal(mini.amount) <= 0:
        raise ValueError(
            f'MiniInvoice {mini.pk} has amount={mini.amount}; '
            f'set the dollar amount before sending')

    _init()
    customer = create_or_get_customer(mini.client)
    stripe.InvoiceItem.create(
        customer=customer.id,
        amount=_cents(mini.amount),
        currency='usd',
        description=mini.description,
    )
    inv = stripe.Invoice.create(
        customer=customer.id,
        collection_method='send_invoice',
        days_until_due=7,
        auto_advance=True,
        metadata={
            'kind': 'mini_invoice',
            'mini_invoice_id': str(mini.id),
            'client_profile_id': str(mini.client_id),
        },
    )
    inv = stripe.Invoice.finalize_invoice(inv.id)
    stripe.Invoice.send_invoice(inv.id)
    mini.stripe_invoice_id = inv.id
    mini.status = 'sent'
    mini.save(update_fields=['stripe_invoice_id', 'status', 'updated_at'])
    logger.info(
        'send_mini_invoice: sent invoice %s for MiniInvoice %s (client %s)',
        inv.id, mini.pk, mini.client_id)
    return inv


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.6 — Reinstatement fee ($75 on 2nd+ payment-failure offense)
# ─────────────────────────────────────────────────────────────────────────────

REINSTATEMENT_FEE = Decimal('75.00')  # CLAUDE.md policy constant


def charge_reinstatement_fee(client, amount=REINSTATEMENT_FEE):
    """Charge the client's default payment method off-session for the
    reinstatement fee. Returns the PaymentIntent on success, None on
    failure (caller must NOT restore the site if this returns None).

    Used by the 2nd+ offense path — 1st offense is free. The off-
    session charge avoids a checkout flow; the saved card from the
    initial onboarding is used."""
    _init()
    customer = create_or_get_customer(client)
    try:
        pi = stripe.PaymentIntent.create(
            customer=customer.id,
            amount=_cents(amount),
            currency='usd',
            off_session=True,
            confirm=True,
            description=f'Reinstatement fee — {client.firm_name}',
            metadata={
                'kind': 'reinstatement_fee',
                'client_profile_id': str(client.id),
                'offense_number': str(client.payment_failure_offenses),
            },
        )
    except stripe.error.CardError as exc:
        # Card declined off-session; admin needs to chase the client
        # for a new payment method before reinstatement can proceed.
        logger.error(
            'charge_reinstatement_fee: card declined for client %s — %s',
            client.pk, exc.user_message)
        return None
    except Exception:
        logger.exception(
            'charge_reinstatement_fee: PI create failed for client %s',
            client.pk)
        return None
    logger.info(
        'charge_reinstatement_fee: charged %s%s to client %s',
        '$', amount, client.pk)
    return pi
