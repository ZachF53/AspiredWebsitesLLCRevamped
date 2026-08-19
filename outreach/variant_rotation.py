"""
Adaptive template-variant rotation (COLD_OUTREACH_AGENT.md §6).

Prospect picks which approved angle to draft a given sequence step from.
This module works out the suggestion; the agent usually follows it but
is not hard-locked to it — genuine per-lead judgement from
``research_lead`` may override, and that override is the point of having
an agent rather than a slot machine.

BE HONEST ABOUT VOLUME
----------------------
At ``daily_send_cap`` = 15 across four steps, with only a fraction of
scraped leads having a usable email address, this business will not
reach statistical significance on copy variants for months. So there is
no frequentist A/B test here, no confidence intervals, and no p-values
that would be lying at this sample size.

What there is:

  * a MOST-ACTIVE default — until there is real data, keep sending the
    variant that already has the most sends rather than splitting thin
    volume across variants and learning nothing about either;
  * a weighted rotation, written and tested but DORMANT, that turns on
    only when every active variant for a step has cleared
    MIN_SAMPLE_SIZE *and* WEIGHTED_ROTATION_ENABLED is flipped on;
  * a floor allocation, so a newer variant is never starved of the data
    it would need to prove itself and an early lucky streak cannot lock
    the rotation onto one angle forever.

Flip ``WEIGHTED_ROTATION_ENABLED`` when send volume actually justifies
it. Do not delete the maths in the meantime — it is the thing that gets
switched on, not rewritten.

(Module named ``variant_rotation`` rather than the brief's ``templates``:
``outreach/templates.py`` sitting next to Django's ``templates/``
directory convention is a confusion this app does not need.)
"""

import logging
import random

logger = logging.getLogger(__name__)


# Sends a variant needs before its reply rate means anything at all.
# Tune upward once real volume exists — 35 is a starting guess, not a
# statistically derived number, and it is deliberately documented as such.
MIN_SAMPLE_SIZE = 35

# Minimum share of traffic every active variant keeps, no matter how
# badly it is performing. Guards against overfitting to an early streak.
FLOOR_ALLOCATION = 0.15

# The master switch. False = always suggest the most-active variant.
# Flip to True once each step's variants have real send volume behind
# them; see the module docstring.
WEIGHTED_ROTATION_ENABLED = False


def active_variants_for(step):
    """Active variants for a sequence step, cheapest-first ordering."""
    from outreach.models import EmailTemplateVariant
    return list(
        EmailTemplateVariant.objects
        .filter(sequence_step=step, active=True)
        .order_by('name')
    )


def compute_weights(variants):
    """Selection weights for a list of variants, summing to 1.0.

    Pure function of the variants' counters — no DB access, no RNG — so
    it is straightforward to test and to render in the admin.

    Below MIN_SAMPLE_SIZE, every variant is weighted equally: there is
    nothing to learn from yet and pretending otherwise is how you overfit
    to three replies. Above it, weight tracks reply rate, with
    FLOOR_ALLOCATION reserved in equal shares for everyone.
    """
    if not variants:
        return {}

    n = len(variants)
    if n == 1:
        return {variants[0].pk: 1.0}

    under_sampled = [v for v in variants if v.sends < MIN_SAMPLE_SIZE]
    if under_sampled:
        # Still gathering. Fair share for all.
        return {v.pk: 1.0 / n for v in variants}

    rates = {v.pk: (v.replies / v.sends if v.sends else 0.0) for v in variants}
    total_rate = sum(rates.values())

    floor_each = FLOOR_ALLOCATION / n
    performance_pool = 1.0 - FLOOR_ALLOCATION

    if total_rate <= 0:
        # Everyone has volume, nobody has replies. Nothing to prefer.
        return {v.pk: 1.0 / n for v in variants}

    return {
        v.pk: floor_each + performance_pool * (rates[v.pk] / total_rate)
        for v in variants
    }


def choose_variant(step, rng=None):
    """Suggest a variant for ``step``.

    Returns ``(variant, reason)``. ``variant`` is None when the step has
    no active variants at all — the caller must treat that as "cannot
    draft this step", not as "draft something".

    ``reason`` is a short human-readable string; it goes into the agent's
    tool result and the run log so an operator reading the trail can see
    why this angle was picked.

    ``rng`` is injectable so tests are deterministic.
    """
    variants = active_variants_for(step)
    if not variants:
        logger.warning(
            'choose_variant: no active EmailTemplateVariant for step %s', step)
        return None, f'No active template variant exists for step {step}.'

    if len(variants) == 1:
        only = variants[0]
        return only, (
            f'"{only.name}" is the only active variant for step {step}.')

    most_active = max(variants, key=lambda v: (v.sends, v.pk))

    if not WEIGHTED_ROTATION_ENABLED:
        return most_active, (
            f'Weighted rotation is off (send volume does not justify it '
            f'yet), so the most-established variant is used: '
            f'"{most_active.name}" with {most_active.sends} sends.')

    under_sampled = [v for v in variants if v.sends < MIN_SAMPLE_SIZE]
    if under_sampled:
        # Still gathering data — favour whichever variant needs it most so
        # every angle reaches MIN_SAMPLE_SIZE as fast as possible.
        neediest = min(under_sampled, key=lambda v: (v.sends, v.pk))
        return neediest, (
            f'"{neediest.name}" has {neediest.sends}/{MIN_SAMPLE_SIZE} sends '
            f'— still gathering a usable sample, so it gets this one.')

    weights = compute_weights(variants)
    picker = rng or random
    chosen = picker.choices(
        variants, weights=[weights[v.pk] for v in variants], k=1)[0]
    return chosen, (
        f'"{chosen.name}" selected by weighted rotation '
        f'(reply rate {chosen.reply_rate:.1%} over {chosen.sends} sends, '
        f'selection weight {weights[chosen.pk]:.0%}).')


def record_send(variant_id):
    """Increment a variant's send counter atomically."""
    _bump(variant_id, 'sends')


def record_open(variant_id):
    """Increment a variant's open counter atomically."""
    _bump(variant_id, 'opens')


def record_reply(variant_id):
    """Increment a variant's reply counter atomically."""
    _bump(variant_id, 'replies')


def record_booking(variant_id):
    """Increment a variant's booking counter atomically."""
    _bump(variant_id, 'bookings')


def _bump(variant_id, field):
    """F()-expression increment so concurrent workers cannot race.

    Never raises: a stats-counter failure must not take down a send.
    """
    if not variant_id:
        return
    try:
        from django.db.models import F

        from outreach.models import EmailTemplateVariant
        EmailTemplateVariant.objects.filter(pk=variant_id).update(
            **{field: F(field) + 1})
    except Exception:  # noqa: BLE001
        logger.exception(
            'Failed to increment %s on EmailTemplateVariant %s',
            field, variant_id)
