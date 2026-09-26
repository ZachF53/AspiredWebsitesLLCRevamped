"""
Charge a current-terms agreement at signing (Sept 2026).

There is no deposit any more. When a client signs, everything the
agreement makes due is charged on the card they enter on our own page
(/pay/contract/<token>/):

  build             plan              Stripe objects created
  ───────────────── ───────────────── ─────────────────────────────────────
  pay in full       none              PaymentIntent $2,000
  pay in full       Full Plan         PaymentIntent $2,000 + Subscription $145/mo
  pay in full       Hosting           PaymentIntent $2,000 + Subscription $45/mo
  installment       none              SubscriptionSchedule: $105/mo x 24, then cancel
  installment       Full Plan         SubscriptionSchedule: $250/mo x 24, then
                                      $145/mo open-ended (ONE subscription)
  installment       Hosting           SubscriptionSchedule $105 x 24 + Subscription $45/mo
  none              Full Plan         Subscription $145/mo
  none              Hosting           Subscription $45/mo

Every price comes from the ServiceTier row's stripe_price_id (or, for the
one-time build, from the ContractService price snapshotted at signing).

Flow, mirroring billing/checkout_views.checkout_confirm: the browser only
ever creates a PaymentMethod (core/static/js/plan_pay.js); the server
attaches it, creates the PaymentIntent / schedules / subscriptions with
payment_behavior=default_incomplete where it applies, and confirms each
first charge server-side. A card that needs 3-D Secure comes back as
``requires_action`` + ``client_secret``; the browser finishes it and lands
on the completion URL, which calls run_contract_checkout() again. Every
step records what it created on ``Contract.stripe_objects`` and skips
itself when already done, so re-running is always safe.
"""

import logging
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)


class CheckoutError(Exception):
    """Buyer-facing failure (declined card, missing Stripe price...)."""


def _g(obj, key, default=None):
    """Read a key off a dict or a stripe 15 StripeObject (no .get())."""
    if obj is None or isinstance(obj, str):
        return default
    try:
        return obj[key] if key in obj else default
    except Exception:  # noqa: BLE001
        return getattr(obj, key, default)


def _stripe():
    from billing.stripe_helpers import _init
    import stripe

    _init()
    return stripe


def _error_message(exc):
    from billing.checkout_views import _stripe_error_message
    return _stripe_error_message(exc)


def _owner(contract):
    return contract.account or contract.client


def _services(contract):
    return {s.service_type: s for s in contract.services.all()}


def _tier(slug):
    from billing.pricing_models import ServiceTier
    return ServiceTier.objects.filter(slug=slug).first()


def _price_id(slug):
    tier = _tier(slug)
    if tier is None or not tier.stripe_price_id:
        raise CheckoutError(
            'This plan is not fully set up for online payment yet. Please '
            'contact zacherylong@aspiredwebsites.com and we will finish it '
            'with you.')
    return tier.stripe_price_id


def _save_objects(contract, objs):
    contract.stripe_objects = objs
    contract.save(update_fields=['stripe_objects', 'updated_at'])


def signing_summary(contract):
    """What the client is charged today and what recurs, for the pay page.

    Returns ``{'today': [(label, Decimal)], 'today_total': Decimal,
    'recurring': [str]}``. Pure DB read — no Stripe calls.
    """
    from clients.contract_options import (
        FULL_PLAN_SLUG, INSTALLMENT_COUNT, PLAN_PAID_IN_FULL_SLUG)

    svc = _services(contract)
    build, plan, hosting = (svc.get('build'), svc.get('maintenance'),
                            svc.get('hosting'))
    today, recurring = [], []
    installment = contract.payment_option == 'installment'
    combined = (installment and plan is not None
                and plan.tier_slug == FULL_PLAN_SLUG)

    if build is not None and contract.payment_option == 'pay_in_full':
        today.append((f'{build.tier_name} — paid in full', build.price))
    if build is not None and installment and not combined:
        today.append((f'{build.tier_name} — installment 1 of '
                      f'{INSTALLMENT_COUNT}', build.price))
        recurring.append(
            f'${build.price:,.2f}/month for the remaining '
            f'{INSTALLMENT_COUNT - 1} build installments '
            f'(${build.price * INSTALLMENT_COUNT:,.2f} total)')
    if plan is not None:
        today.append((f'{plan.tier_name} — first month', plan.price))
        if combined:
            step = _tier(PLAN_PAID_IN_FULL_SLUG)
            step_price = step.price if step is not None else None
            recurring.append(
                f'${plan.price:,.2f}/month for months 2–{INSTALLMENT_COUNT} '
                f'(includes the ${build.price:,.2f} build installment)'
                + (f', then ${step_price:,.2f}/month, month-to-month'
                   if step_price is not None else ''))
        else:
            recurring.append(
                f'${plan.price:,.2f}/month, month-to-month')
    if hosting is not None:
        today.append((f'{hosting.tier_name} — first month', hosting.price))
        recurring.append(f'${hosting.price:,.2f}/month, month-to-month')
    total = sum((amt for _, amt in today), Decimal('0'))
    return {'today': today, 'today_total': total, 'recurring': recurring}


# ── Stripe steps ────────────────────────────────────────────────────────────

def _settle_subscription(stripe, sub_id, pm):
    """Make sure a subscription's FIRST invoice is paid.

    Returns ('paid'|'requires_action'|'pending', client_secret or None).
    Raises CheckoutError on a decline.
    """
    sub = stripe.Subscription.retrieve(
        sub_id, expand=['latest_invoice.confirmation_secret'])
    if _g(sub, 'status') in ('active', 'trialing'):
        return 'paid', None
    inv = _g(sub, 'latest_invoice')
    if inv is None or isinstance(inv, str):
        return 'pending', None
    if _g(inv, 'status') == 'paid':
        return 'paid', None
    if _g(inv, 'status') == 'draft':
        inv = stripe.Invoice.finalize_invoice(
            _g(inv, 'id'), expand=['confirmation_secret'])
        if _g(inv, 'status') == 'paid':
            return 'paid', None
    secret = _g(_g(inv, 'confirmation_secret'), 'client_secret')
    if not secret:
        return 'pending', None
    return _settle_payment_intent(
        stripe, secret.split('_secret', 1)[0], pm, secret)


def _settle_payment_intent(stripe, pi_id, pm, secret=None):
    pi = stripe.PaymentIntent.retrieve(pi_id)
    status = _g(pi, 'status')
    if status in ('requires_payment_method', 'requires_confirmation'):
        try:
            pi = stripe.PaymentIntent.confirm(pi_id, payment_method=pm)
        except Exception as exc:  # noqa: BLE001 — decline etc.
            raise CheckoutError(_error_message(exc)) from exc
        status = _g(pi, 'status')
    if status in ('succeeded', 'processing', 'requires_capture'):
        return 'paid', None
    if status == 'requires_action':
        return 'requires_action', secret or _g(pi, 'client_secret')
    raise CheckoutError('Your payment could not be completed. Please try '
                        'another card.')


def _meta(contract, kind):
    """Subscription metadata. Deliberately NOT tier_slug/product_type:
    those keys make customer.subscription.created treat it as a
    self-checkout sale and provision a second account pass."""
    web = contract.website_new
    return {
        'source': 'aspired_websites',
        'aspired_kind': kind,
        'contract_id': str(contract.id),
        'website_id': str(web.id) if web is not None else '',
    }


def _charge_build_in_full(stripe, contract, objs, customer_id, pm):
    """PaymentIntent for the whole build, through an OnboardingInvoice so
    the existing payment_intent.succeeded path marks the site fully paid,
    writes the ledger row and sends the branded receipt."""
    from billing.stripe_helpers import _cents
    from clients.models import OnboardingInvoice

    build = _services(contract)['build']
    pi_id = objs.get('build_payment_intent')
    if pi_id:
        return _settle_payment_intent(stripe, pi_id, pm)

    desc = f'{build.tier_name} — paid in full'
    invoice = OnboardingInvoice.objects.create(
        client=contract.client, contract=contract, is_deposit=False,
        account_new=contract.account, website_new=contract.website_new,
        line_items=[{'description': desc, 'amount': f'{build.price:.2f}'}],
        total_amount=build.price, status='draft')
    objs['build_invoice'] = str(invoice.id)
    _save_objects(contract, objs)
    owner = _owner(contract)
    try:
        pi = stripe.PaymentIntent.create(
            amount=_cents(build.price), currency='usd',
            customer=customer_id, payment_method=pm,
            payment_method_types=['card'], confirm=True,
            setup_future_usage='off_session',
            description=f'Aspired Websites — {desc}'[:1000],
            metadata={
                'source': 'aspired_websites', 'kind': 'onboarding',
                'client_profile_id': str(owner.id),
                'invoice_id': str(invoice.id),
                'contract_id': str(contract.id)})
    except Exception as exc:  # noqa: BLE001
        # A decline still leaves a PaymentIntent behind; keep it so the
        # retry confirms that one instead of creating a second charge.
        err_pi = _g(_g(getattr(exc, 'error', None), 'payment_intent'), 'id')
        if err_pi:
            objs['build_payment_intent'] = err_pi
            _save_objects(contract, objs)
            invoice.stripe_payment_intent_id = err_pi
            invoice.save(update_fields=['stripe_payment_intent_id',
                                        'updated_at'])
        raise CheckoutError(_error_message(exc)) from exc

    objs['build_payment_intent'] = pi.id
    _save_objects(contract, objs)
    invoice.stripe_payment_intent_id = pi.id
    invoice.stripe_client_secret = _g(pi, 'client_secret') or ''
    invoice.status = 'sent'
    invoice.sent_at = timezone.now()
    invoice.save(update_fields=[
        'stripe_payment_intent_id', 'stripe_client_secret', 'status',
        'sent_at', 'updated_at'])
    return _settle_payment_intent(stripe, pi.id, pm)


def _create_schedule(stripe, contract, customer_id, pm, phases,
                     end_behavior, kind):
    meta = _meta(contract, kind)
    for phase in phases:
        phase['metadata'] = meta
    schedule = stripe.SubscriptionSchedule.create(
        customer=customer_id, start_date='now', end_behavior=end_behavior,
        default_settings={'default_payment_method': pm,
                          'collection_method': 'charge_automatically'},
        phases=phases, metadata=meta)
    sub = _g(schedule, 'subscription')
    sub_id = sub if isinstance(sub, str) else _g(sub, 'id')
    return _g(schedule, 'id'), sub_id


def _create_subscription(stripe, contract, customer_id, pm, price_id, kind):
    sub = stripe.Subscription.create(
        customer=customer_id, items=[{'price': price_id}],
        default_payment_method=pm,
        payment_behavior='default_incomplete',
        payment_settings={'save_default_payment_method': 'on_subscription'},
        metadata=_meta(contract, kind))
    return sub.id


def _start_installments(stripe, contract, objs, customer_id, pm):
    """The 24-installment schedule. Combined with a Full Plan it is ONE
    schedule: hvac-full-plan x 24, then hvac-plan-paid-in-full open-ended."""
    from clients.contract_options import (
        FULL_PLAN_SLUG, INSTALLMENT_COUNT, PLAN_PAID_IN_FULL_SLUG)

    svc = _services(contract)
    plan = svc.get('maintenance')
    combined = plan is not None and plan.tier_slug == FULL_PLAN_SLUG
    if not objs.get('installment_subscription'):
        duration = {'interval': 'month', 'interval_count': INSTALLMENT_COUNT}
        if combined:
            phases = [
                {'items': [{'price': _price_id(FULL_PLAN_SLUG)}],
                 'duration': duration},
                {'items': [{'price': _price_id(PLAN_PAID_IN_FULL_SLUG)}]},
            ]
            # 'release' leaves the $145 subscription running on its own
            # once phase 2 starts — month-to-month, cancellable.
            end_behavior = 'release'
        else:
            phases = [{'items': [{'price': _price_id(
                svc['build'].tier_slug)}], 'duration': duration}]
            end_behavior = 'cancel'
        try:
            schedule_id, sub_id = _create_schedule(
                stripe, contract, customer_id, pm, phases, end_behavior,
                'installment')
        except CheckoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception('contract %s: schedule create failed',
                             contract.pk)
            raise CheckoutError(_error_message(exc)) from exc
        objs['installment_schedule'] = schedule_id
        objs['installment_subscription'] = sub_id
        if combined:
            objs['plan_subscription'] = sub_id
            objs['plan_schedule'] = schedule_id
        _save_objects(contract, objs)
        _record_installment_locally(contract, schedule_id, sub_id,
                                    combined)
    return _settle_subscription(stripe, objs['installment_subscription'], pm)


def _record_installment_locally(contract, schedule_id, sub_id, combined):
    web = contract.website_new
    if web is not None:
        web.stripe_build_installment_subscription_id = sub_id
        web.stripe_build_installment_schedule_id = schedule_id
        fields = ['stripe_build_installment_subscription_id',
                  'stripe_build_installment_schedule_id']
        if combined:
            web.stripe_maintenance_subscription_id = sub_id
            fields.append('stripe_maintenance_subscription_id')
        web.save(update_fields=fields + ['updated_at'])
    if combined:
        _record_plan_row(contract, sub_id)


def _record_plan_row(contract, sub_id):
    from clients.service_models import MaintenancePlan

    plan_svc = _services(contract).get('maintenance')
    account = contract.account
    if plan_svc is None or account is None:
        return
    MaintenancePlan.objects.get_or_create(
        stripe_subscription_id=sub_id,
        defaults={'account': account, 'website': contract.website_new,
                  'tier_slug': plan_svc.tier_slug,
                  'status': 'awaiting_payment'})


def _start_plan(stripe, contract, objs, customer_id, pm):
    if not objs.get('plan_subscription'):
        plan = _services(contract)['maintenance']
        try:
            sub_id = _create_subscription(
                stripe, contract, customer_id, pm,
                _price_id(plan.tier_slug), 'plan')
        except CheckoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CheckoutError(_error_message(exc)) from exc
        objs['plan_subscription'] = sub_id
        _save_objects(contract, objs)
        web = contract.website_new
        if web is not None:
            web.stripe_maintenance_subscription_id = sub_id
            web.save(update_fields=['stripe_maintenance_subscription_id',
                                    'updated_at'])
        _record_plan_row(contract, sub_id)
    return _settle_subscription(stripe, objs['plan_subscription'], pm)


def _start_hosting(stripe, contract, objs, customer_id, pm):
    if not objs.get('hosting_subscription'):
        hosting = _services(contract)['hosting']
        try:
            sub_id = _create_subscription(
                stripe, contract, customer_id, pm,
                _price_id(hosting.tier_slug), 'hosting')
        except CheckoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CheckoutError(_error_message(exc)) from exc
        objs['hosting_subscription'] = sub_id
        _save_objects(contract, objs)
        web = contract.website_new
        if web is not None:
            web.stripe_hosting_subscription_id = sub_id
            web.save(update_fields=['stripe_hosting_subscription_id',
                                    'updated_at'])
    return _settle_subscription(stripe, objs['hosting_subscription'], pm)


# ── Orchestration ───────────────────────────────────────────────────────────

def run_contract_checkout(contract, payment_method_id=None):
    """Charge everything due at signing. Idempotent; safe to re-run.

    Returns one of ``{'ok': True}``, ``{'requires_action': True,
    'client_secret': ...}`` or ``{'error': '...'}``.
    """
    from billing.stripe_helpers import StripeNotConfigured

    if contract.is_legacy_billing:
        return {'error': 'This agreement uses the invoice payment page.'}
    if not contract.signed:
        return {'error': 'Please sign the agreement first.'}
    if contract.paid_at_signing_at:
        return {'ok': True}
    owner = _owner(contract)
    if owner is None:
        return {'error': 'This agreement has no account to bill.'}

    try:
        stripe = _stripe()
    except StripeNotConfigured:
        return {'error': 'Online payment is not available right now. '
                         'Please contact zacherylong@aspiredwebsites.com.'}

    objs = dict(contract.stripe_objects or {})
    try:
        from billing.stripe_helpers import create_or_get_customer
        customer = create_or_get_customer(owner)
    except Exception:  # noqa: BLE001
        logger.exception('contract %s: customer lookup failed', contract.pk)
        return {'error': 'We could not set up your billing profile. '
                         'Please try again in a minute.'}
    customer_id = customer.id

    pm = payment_method_id or objs.get('payment_method')
    if not pm:
        return {'error': 'Please enter a card.'}
    if payment_method_id and payment_method_id != objs.get('payment_method'):
        try:
            stripe.PaymentMethod.attach(payment_method_id,
                                        customer=customer_id)
            stripe.Customer.modify(
                customer_id,
                invoice_settings={
                    'default_payment_method': payment_method_id})
        except Exception as exc:  # noqa: BLE001 — declines land here
            logger.info('contract %s: card attach failed', contract.pk)
            return {'error': _error_message(exc)}
        objs['payment_method'] = payment_method_id
        objs['customer'] = customer_id
        _save_objects(contract, objs)

    svc = _services(contract)
    steps = []
    if contract.payment_option == 'pay_in_full' and 'build' in svc:
        steps.append(_charge_build_in_full)
    if contract.payment_option == 'installment' and 'build' in svc:
        steps.append(_start_installments)
    if 'maintenance' in svc:
        steps.append(_start_plan)  # no-op when the schedule already covers it
    if 'hosting' in svc:
        steps.append(_start_hosting)

    for step in steps:
        try:
            state, secret = step(stripe, contract, objs, customer_id, pm)
        except CheckoutError as exc:
            return {'error': str(exc)}
        except Exception:  # noqa: BLE001
            logger.exception('contract %s: %s failed', contract.pk,
                             step.__name__)
            return {'error': 'Something went wrong starting your payment. '
                             'Nothing further was charged — please try '
                             'again or contact us.'}
        if state == 'requires_action':
            return {'requires_action': True, 'client_secret': secret}

    complete_contract_checkout(contract)
    return {'ok': True}


def complete_contract_checkout(contract):
    """Local state once everything due at signing has been charged.

    Mirrors what the deposit path did on payment: activate the user,
    unlock intake, email the setup link, start the build lifecycle.
    The pay-in-full PaymentIntent is also processed here synchronously
    (the webhook does the same thing idempotently) so the client's
    next page never depends on webhook timing.
    """
    from billing import webhooks
    from clients.service_models import MaintenancePlan

    objs = contract.stripe_objects or {}
    now = timezone.now()

    # Plan rows created at awaiting_payment → active now the first
    # invoice is paid (invoice.paid does the same, idempotently).
    for key in ('plan_subscription',):
        sub_id = objs.get(key)
        if sub_id:
            for plan in MaintenancePlan.objects.filter(
                    stripe_subscription_id=sub_id,
                    status='awaiting_payment'):
                plan.status = 'active'
                plan.started_at = plan.started_at or now
                plan.save(update_fields=['status', 'started_at',
                                         'updated_at'])

    web = contract.website_new
    if web is not None:
        fields = []
        if objs.get('plan_subscription') and not web.maintenance_active:
            web.maintenance_active = True
            web.maintenance_started_at = web.maintenance_started_at or now
            fields += ['maintenance_active', 'maintenance_started_at']
        if contract.includes_build:
            if contract.payment_option == 'installment':
                web.payment_status = 'installments_active'
                web.deposit_paid_at = web.deposit_paid_at or now
                fields += ['payment_status', 'deposit_paid_at']
            web.lifecycle_status = 'deposit_paid'
            if not web.stage:
                web.stage = 'intake'
            fields += ['lifecycle_status', 'stage']
        if fields:
            web.save(update_fields=fields + ['updated_at'])

    pi_id = objs.get('build_payment_intent')
    if pi_id:
        try:
            stripe = _stripe()
            pi = stripe.PaymentIntent.retrieve(pi_id)
            if _g(pi, 'status') == 'succeeded':
                webhooks._handle_payment_intent_succeeded(
                    {'data': {'object': (pi.to_dict() if hasattr(
                        pi, 'to_dict') else dict(pi))}})
        except Exception:  # noqa: BLE001 — the webhook backstops this
            logger.exception('contract %s: sync build-payment processing '
                             'failed', contract.pk)
    else:
        # No build PaymentIntent (installment / plan-only): run the same
        # onboarding the invoice path runs — activate the user, bootstrap
        # intake + vault, email the account-setup link.
        account = contract.account
        if account is not None:
            try:
                webhooks._on_onboarding_invoice_paid(account, None)
                if web is not None and contract.includes_build:
                    from clients.models import IntakeResponse
                    IntakeResponse.objects.get_or_create(website_new=web)
            except Exception:  # noqa: BLE001
                logger.exception('contract %s: onboarding after signing '
                                 'payment failed', contract.pk)

    contract.paid_at_signing_at = now
    contract.save(update_fields=['paid_at_signing_at', 'updated_at'])
    try:
        from clients.emails import send_contract_signed_email
        send_contract_signed_email(contract)
    except Exception:  # noqa: BLE001
        logger.exception('contract %s: signed email failed', contract.pk)


def next_url_after_checkout(contract):
    """Where the client goes once everything is paid: the account-setup
    link when they have not set a password yet, else their portal."""
    owner = _owner(contract)
    token = (getattr(owner, 'onboarding_token_new', None)
             or getattr(owner, 'onboarding_token', None))
    try:
        if token is not None and not token.used:
            return token.get_setup_url()
    except Exception:  # noqa: BLE001
        pass
    return '/portal/'
