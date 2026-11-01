"""
The 14-day free trial path: SetupIntent first, subscription second.

## Why this is not Stripe Checkout

The brief that prompted this described `consent_collection`,
`payment_method_collection`, `success_url` and
`{CHECKOUT_SESSION_ID}` — all Stripe-hosted Checkout parameters. This
site does not use hosted Checkout. `billing/checkout_views.py` runs a
custom Stripe Elements form, creates the Customer and Subscription
server-side, and confirms the first invoice's PaymentIntent itself.
None of those parameters exist here, so the trial is built the
equivalent way: confirm a SetupIntent to collect and authenticate the
card, then create a Subscription with `trial_period_days`.

## Why the SetupIntent comes first

Adding `trial_period_days` to the existing charge-now path looks like a
one-line change and is not. With a trial the first invoice is $0, so
Stripe creates no PaymentIntent and the invoice has no
`confirmation_secret`. The existing code at checkout_views step 5/6
would find `client_secret = None`, skip `PaymentIntent.confirm`
entirely, and return `status: None` — which falls through to the
success branch. The sale would look fine.

The damage shows up 14 days later. The card was attached but never
authenticated, so a card that needs 3DS fails its first real charge on
day 15, after the customer has configured a receptionist and forwarded
their phone line. A SetupIntent moves that failure back to signup,
where the buyer is present and can fix it.

It also creates the off-session mandate Stripe wants for the day-15
charge. Attaching a PaymentMethod alone does not.

## Order of operations, and why it is this order

1. TermsAcceptance row (before any network call, see terms_models).
2. Customer get-or-create, attach card, make it the default.
3. SetupIntent, confirmed server-side with that card.
4. Subscription with `trial_period_days`.

Steps 3 and 4 are split by an SCA round trip when the card demands one,
which is why `start_trial_subscription` is callable twice: once from
`checkout_confirm` when the SetupIntent succeeds outright, and once
from `checkout_finalize` after the browser has completed 3DS. The
TermsAcceptance id is the only thing passed between them. The finalize
endpoint re-reads email and customer id off that row rather than
trusting the browser, so a tampered POST cannot point a subscription at
someone else's Stripe customer.

A subscription is never created from an unconfirmed SetupIntent. The
failure mode we want is "no trial started, buyer sees an error", not
"trial started on a card that cannot be charged".
"""

import logging

from django.conf import settings
from django.utils import timezone

from billing.terms_models import TermsAcceptance

logger = logging.getLogger(__name__)


# The two products sold on a free trial. Both are $97/mo add-ons and
# both are self-checkout-able; nothing else on the site trials.
#
# Keyed off the same constants as the rest of the codebase rather than
# re-typing the slugs, so a slug rename cannot leave the trial pointing
# at a tier that no longer exists.
def trial_slugs():
    from clients.contract_options import (
        REVIEW_SYSTEM_SLUG, VOICE_RECEPTIONIST_SLUG,
    )
    return (VOICE_RECEPTIONIST_SLUG, REVIEW_SYSTEM_SLUG)


def tier_is_trial_eligible(tier):
    """Whether this tier starts as a free trial rather than charging now.

    Everything else on the site — maintenance, hosting, social media —
    keeps the existing charge-immediately path untouched. Narrowing the
    change to two slugs is deliberate: the charge-now path handles
    coupons, the hosting upsell and installment builds, and none of
    that needs to move.
    """
    return bool(tier) and tier.slug in trial_slugs()


def trial_days():
    """Trial length, floored at 1.

    A 0 or negative TRIAL_DAYS would make Stripe charge immediately
    while every disclosure on the page still promised a free trial.
    Refusing to honour a nonsensical value is better than quietly
    charging someone who was told they would not be.
    """
    try:
        days = int(getattr(settings, 'TRIAL_DAYS', 14))
    except (TypeError, ValueError):
        days = 14
    return max(1, days)


def trial_end_date(*, start=None):
    """The date the first charge lands, computed server-side.

    Used for the pre-purchase disclosure, so the date the buyer is shown
    is derived from the same setting Stripe is handed rather than typed
    into a template.

    This is the expected date. The authoritative one is `trial_end` on
    the subscription Stripe actually creates, which is what the
    trial-started page reads back.
    """
    from datetime import timedelta
    start = start or timezone.now()
    return (start + timedelta(days=trial_days())).date()


def client_ip(request):
    """Best-effort client IP for the acceptance record.

    Behind nginx the useful value is the first entry of
    X-Forwarded-For; REMOTE_ADDR alone is the proxy. Returns None rather
    than '' so a missing IP is distinguishable from an unparseable one,
    and so GenericIPAddressField stores NULL instead of raising on a
    malformed header.
    """
    forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').strip()
    candidate = (forwarded.split(',')[0].strip() if forwarded
                 else (request.META.get('REMOTE_ADDR') or '').strip())
    if not candidate:
        return None
    # Never trust a header to be an address. An unvalidated value raises
    # on save and would take the whole purchase down with it.
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_ipv46_address
    try:
        validate_ipv46_address(candidate)
    except ValidationError:
        logger.warning('trials: unparseable client IP %r', candidate[:80])
        return None
    return candidate


def record_acceptance(request, *, email, tier):
    """Write the TermsAcceptance row. Called before any Stripe call.

    Returns the row. Never silently skipped: if this fails, the caller
    must abort rather than take a card, because the acceptance evidence
    is the thing that makes the trial defensible.
    """
    return TermsAcceptance.objects.create(
        email=email,
        product_slug=tier.slug,
        terms_version=getattr(settings, 'TERMS_VERSION', ''),
        accepted_at=timezone.now(),
        ip_address=client_ip(request),
        # Truncated rather than unbounded: it is a TextField, but a
        # hostile client can send megabytes and this runs before any
        # rate limit has a chance to care.
        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:1000],
    )


def subscription_metadata(acceptance, *, tier, product_type,
                          onboarding_product_type, ghl_contact_id=''):
    """Metadata stamped on the subscription and the SetupIntent.

    `site_lead_id` is the TermsAcceptance UUID, which is what the
    trial-started page is keyed on and what the webhook uses to find
    the row to complete. Every value is str()'d because Stripe rejects
    non-string metadata values, and a UUID or a date silently becomes a
    400 at subscription-create time otherwise.
    """
    return {
        'product_slug': tier.slug,
        'terms_version': acceptance.terms_version,
        'terms_accepted_at': acceptance.accepted_at.isoformat(),
        'terms_accepted_ip': str(acceptance.ip_address or ''),
        'ghl_contact_id': str(ghl_contact_id or ''),
        'site_lead_id': str(acceptance.pk),
        # Read by the existing provisioning backstop in
        # billing/webhooks.py. Without these the subscription.created
        # handler early-returns and the buyer gets no account.
        'tier_slug': tier.slug,
        'product_type': product_type,
        'onboarding_product_type': onboarding_product_type,
        'hosting_upsell': '0',
        # Marks this subscription as trial-originated for the webhook,
        # which must tell a trial start apart from an ordinary paid
        # subscription.created to decide whether to push trial-started
        # to GHL.
        'trial_signup': '1',
    }


def confirm_card_setup(stripe, *, customer_id, payment_method_id, metadata):
    """Create and confirm a SetupIntent for the card just collected.

    Returns the SetupIntent. Raises whatever Stripe raises — a card that
    cannot be set up must not become a trial.

    `usage='off_session'` is the point of the exercise: it records the
    mandate that lets Stripe charge this card on day 15 without the
    customer present. A PaymentMethod merely attached to a Customer has
    no such mandate, and some issuers decline the first off-session
    charge without one.
    """
    return stripe.SetupIntent.create(
        customer=customer_id,
        payment_method=payment_method_id,
        confirm=True,
        usage='off_session',
        # The card is already collected by Elements, so there is nowhere
        # to redirect to and no redirect-based method in play. Without
        # this, Stripe can return a redirect-type next_action that our
        # confirmCardSetup call cannot complete.
        automatic_payment_methods={'enabled': True,
                                   'allow_redirects': 'never'},
        metadata=metadata,
    )


def start_trial_subscription(stripe, *, acceptance, tier, payment_method_id,
                             product_type, onboarding_product_type,
                             ghl_contact_id=''):
    """Create the trialing subscription. Returns the Stripe Subscription.

    Assumes the card is already attached and its SetupIntent confirmed.
    Callable from both the direct path and the post-SCA finalize path;
    the TermsAcceptance row carries the customer id between them.

    No `payment_behavior` and no PaymentIntent confirmation, unlike the
    charge-now path: there is nothing to charge. Stripe owns the trial
    clock and the day-15 conversion from here.
    """
    metadata = subscription_metadata(
        acceptance, tier=tier, product_type=product_type,
        onboarding_product_type=onboarding_product_type,
        ghl_contact_id=ghl_contact_id)

    subscription = stripe.Subscription.create(
        customer=acceptance.stripe_customer_id,
        items=[{'price': tier.stripe_price_id}],
        trial_period_days=trial_days(),
        default_payment_method=payment_method_id,
        # If the card is gone by the time the trial ends, cancel rather
        # than drop the subscription into an unpaid state that keeps a
        # receptionist answering calls nobody is paying for.
        trial_settings={
            'end_behavior': {'missing_payment_method': 'cancel'},
        },
        payment_settings={
            'save_default_payment_method': 'on_subscription',
        },
        metadata=metadata,
    )

    # Stamp the ids back onto the acceptance row immediately. The
    # webhook does this too, but it is async and may be seconds or
    # minutes behind, and the trial-started page needs the subscription
    # id the moment the buyer lands on it.
    acceptance.stripe_subscription_id = subscription['id']
    acceptance.save(update_fields=['stripe_subscription_id', 'updated_at'])

    logger.info(
        'trials: started %s-day trial for %s on %s (sub=%s acceptance=%s)',
        trial_days(), acceptance.email, tier.slug, subscription['id'],
        acceptance.pk)
    return subscription


def subscription_trial_end(subscription):
    """`trial_end` off a Stripe Subscription as an aware datetime, or None.

    Stripe objects are not dicts — stripe 15's StripeObject has no
    .get() — so membership is tested with `in` and read by subscripting.
    This is the authoritative first-charge date, as opposed to the one
    computed from TRIAL_DAYS for the pre-purchase disclosure.
    """
    if subscription is None or 'trial_end' not in subscription:
        return None
    raw = subscription['trial_end']
    if not raw:
        return None
    from datetime import datetime, timezone as dt_timezone
    try:
        return datetime.fromtimestamp(int(raw), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError):
        logger.warning('trials: unreadable trial_end %r', raw)
        return None
