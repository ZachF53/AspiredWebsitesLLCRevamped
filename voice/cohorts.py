"""
The receptionist price ladder: $97 for 10 accounts, then +$50 per 10.

## How grandfathering actually works

Mostly, Stripe does it for us. A subscription stays attached to the Price
object it was created with for its whole life, so escalating the ladder
does not touch anyone who already signed up. There is no proration, no
migration, and no "please accept the new terms" email. We only have to
stop pointing NEW signups at the old Price.

That makes escalation three writes:

1. Mint a new Stripe Price at the new amount (same Product).
2. Open cohort N+1 and close cohort N.
3. Repoint ``ServiceTier.price`` and ``ServiceTier.stripe_price_id``.

Step 3 is why no display code knows cohorts exist. Every public, portal
and checkout surface already reads ``tier.get_price_display`` and
``tier.stripe_price_id``; repointing that one row makes the entire site
start quoting and charging the new price at once.

## Comps consume slots

Owner's decision, Oct 2026: a comped account takes a slot like a paying
one. So ``filled`` counts ``VoiceProgram`` rows (which exist for comps
too) rather than Stripe subscriptions. The practical consequence is
deliberate — comping yourself and one existing free client puts 2 real
accounts on the board, so the public "8 of 10 spots left" counter is
honest rather than a seeded fiction.

## Why slots_remaining is cached

It is a COUNT(*) that renders on /pricing/, the service page, the home
page trust block and the portal signup card. Uncached, every anonymous
pricing view is a database round trip for a number that changes a handful
of times a year. 60 seconds is short enough that the counter never looks
stale to a buyer and long enough to take it off the hot path.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from voice.models import VoiceCohort

logger = logging.getLogger(__name__)

# The step between rungs. Not a setting: changing it mid-ladder would
# make the already-created rungs disagree with the rule that generated
# them, and the fix for "I want a different step" is to edit the next
# cohort row directly rather than to re-derive history.
STEP = Decimal('50.00')

OPENING_PRICE = Decimal('97.00')
DEFAULT_CAPACITY = 10

_SLOTS_CACHE_KEY = 'voice:cohort:slots'
_SLOTS_CACHE_TTL = 60


def _tier():
    from billing.pricing_models import ServiceTier
    from clients.contract_options import VOICE_RECEPTIONIST_SLUG
    return ServiceTier.objects.filter(
        slug=VOICE_RECEPTIONIST_SLUG).first()


def _adopt_tier_price_id(cohort):
    """Backfill a cohort's Stripe price id from the tier, once.

    The opening cohort is created by ``voice/migrations/0002``, which
    runs at deploy time — before ``sync_stripe_products`` has ever been
    run, so the tier's ``stripe_price_id`` is still blank and the cohort
    copies a blank. Without this the ladder's first rung has no record of
    what it actually sold at, and the moment it escalates there is no way
    to tell which Stripe Price the grandfathered clients are on.

    Only ever fills a blank. A cohort that already names a price keeps
    it, because after an escalation the tier points at the NEXT rung's
    price and adopting that would rewrite history.
    """
    if cohort.stripe_price_id:
        return cohort
    tier = _tier()
    if tier is None or not tier.stripe_price_id:
        return cohort
    cohort.stripe_price_id = tier.stripe_price_id
    cohort.save(update_fields=['stripe_price_id', 'updated_at'])
    logger.info('voice.cohorts: adopted tier price id for cohort %s',
                cohort.sequence)
    return cohort


def current_cohort(*, create=True):
    """The rung new signups land on.

    Self-heals a database with no cohort rows at all, which is the state
    a fresh install or a test fixture is in. Without that, every caller
    would need its own "if the ladder isn't set up" branch.
    """
    cohort = VoiceCohort.objects.filter(is_current=True).first()
    if cohort is not None:
        _adopt_tier_price_id(cohort)
        return cohort
    if not create:
        return None
    # No ladder yet. Open rung 1 at whatever the tier currently says, so
    # an operator who edited the price in the admin dashboard before the
    # first sale gets the price they chose rather than a hardcoded $97.
    tier = _tier()
    price = tier.price if tier is not None else OPENING_PRICE
    stripe_price_id = tier.stripe_price_id if tier is not None else ''
    cohort = VoiceCohort.objects.create(
        sequence=1, price=price, stripe_price_id=stripe_price_id,
        capacity=DEFAULT_CAPACITY, is_current=True,
        opened_at=timezone.now(),
    )
    logger.info('voice.cohorts: opened cohort 1 at %s', price)
    return cohort


def slots_remaining():
    """Slots left in the current cohort, or None if there is no ladder.

    None (rather than 0) means "don't render the scarcity bar" — a
    missing ladder is a configuration problem, and advertising "0 spots
    left" to a buyer because a migration didn't run would cost a sale.
    """
    cached = cache.get(_SLOTS_CACHE_KEY)
    if cached is not None:
        return cached if cached >= 0 else None
    cohort = current_cohort(create=False)
    value = cohort.slots_remaining if cohort is not None else -1
    cache.set(_SLOTS_CACHE_KEY, value, _SLOTS_CACHE_TTL)
    return value if value >= 0 else None


def bust_slots_cache():
    cache.delete(_SLOTS_CACHE_KEY)


def cohort_context():
    """What the public and portal scarcity bar needs, or {} to hide it.

    Returns `next_price` so the copy can be specific — "then it goes to
    $147" converts better than "then the price increases", and it is also
    simply more honest.
    """
    cohort = current_cohort(create=False)
    if cohort is None:
        return {}
    remaining = cohort.slots_remaining
    if remaining <= 0:
        # Cohort is full but has not escalated yet (the escalation runs
        # on the next grant). Quoting "0 spots left at $97" would be
        # wrong in both directions, so say nothing.
        return {}
    return {
        'capacity': cohort.capacity,
        'slots_remaining': remaining,
        'price': cohort.price,
        'next_price': cohort.price + STEP,
        'sequence': cohort.sequence,
    }


def assign_to_current(program):
    """Put a newly entitled program on the current rung.

    Idempotent: a program that already has a cohort keeps it. That
    matters because this is called from both the purchase path and the
    comp path, and a comp toggled off and on again must not silently
    reprice the client.
    """
    if program.cohort_id is not None:
        return program.cohort
    cohort = current_cohort()
    program.cohort = cohort
    program.locked_price = cohort.price
    program.save(update_fields=['cohort', 'locked_price', 'updated_at'])
    bust_slots_cache()
    return cohort


def _mint_stripe_price(tier, amount):
    """Create the next rung's Stripe Price on the existing Product.

    Returns '' when Stripe is unconfigured or the tier has no Product
    yet. An empty price id is not fatal here: the cohort still opens and
    ``ServiceTier`` still shows the new amount, but checkout_confirm
    already refuses a tier with no Stripe price rather than taking a
    payment it cannot bill, so the failure mode is "nobody can buy until
    sync_stripe_products runs" rather than "someone is charged the wrong
    amount".
    """
    if not settings.STRIPE_SECRET_KEY or not tier or not tier.stripe_product_id:
        return ''
    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        price = stripe.Price.create(
            product=tier.stripe_product_id,
            unit_amount=int(amount * 100),
            currency='usd',
            recurring={'interval': tier.billing_interval or 'month'},
            metadata={'tier_slug': tier.slug, 'voice_cohort_price': str(amount)},
        )
        return price.id
    except Exception:
        logger.exception('voice.cohorts: Stripe Price create failed at %s',
                         amount)
        return ''


@transaction.atomic
def maybe_escalate():
    """Close a full cohort and open the next one.

    Called after every entitlement grant — purchase or comp. A no-op
    unless the current rung is full, so callers do not need to check.

    Wrapped in a transaction and selected FOR UPDATE because two
    concurrent checkouts both completing the 10th slot would otherwise
    both try to open cohort N+1 and one would violate the partial unique
    constraint on `is_current`. Serialising the read makes the loser see
    the already-opened rung instead.
    """
    cohort = (VoiceCohort.objects
              .select_for_update()
              .filter(is_current=True)
              .first())
    if cohort is None or not cohort.is_full:
        return None

    new_price = cohort.price + STEP
    tier = _tier()
    stripe_price_id = _mint_stripe_price(tier, new_price)

    now = timezone.now()
    cohort.is_current = False
    cohort.closed_at = now
    cohort.save(update_fields=['is_current', 'closed_at', 'updated_at'])

    nxt = VoiceCohort.objects.create(
        sequence=cohort.sequence + 1, price=new_price,
        stripe_price_id=stripe_price_id, capacity=cohort.capacity,
        is_current=True, opened_at=now,
    )

    # Repoint the tier. This is the write that makes the whole site
    # quote the new price. Leaving stripe_price_id alone when minting
    # failed is deliberate — better to block new sales than to charge
    # $97 for a $147 product.
    if tier is not None:
        fields = ['price', 'updated_at']
        tier.price = new_price
        if stripe_price_id:
            tier.stripe_price_id = stripe_price_id
            fields.append('stripe_price_id')
        tier.save(update_fields=fields)

    bust_slots_cache()
    logger.info('voice.cohorts: escalated cohort %s -> %s at %s (price %s)',
                cohort.sequence, nxt.sequence, new_price,
                stripe_price_id or 'UNMINTED')
    return nxt
