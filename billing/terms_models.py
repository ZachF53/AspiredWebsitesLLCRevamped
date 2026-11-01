"""
Proof that a buyer accepted the Terms of Service before we took a card.

A free trial that converts to a paid subscription needs three things to
be defensible: clear disclosure before the card is taken, a reminder
before the first charge, and cancellation at least as easy as signup.
This table is the evidence for the first one. It answers, for any
subscription, "what exactly did this person agree to, when, and from
where" without anyone having to reconstruct it from Stripe metadata and
a template's git history.

## Why the row is written BEFORE Stripe is called

The acceptance is a fact about the buyer's browser, and it stops being
observable the moment we redirect. Writing it first means a Stripe
failure (declined card, network timeout, SetupIntent refused) still
leaves a record that this person read and accepted version X at this
time, which is also how we tell "accepted but did not buy" apart from
"never saw the terms". The Stripe ids are therefore nullable and get
filled in later: ``stripe_customer_id`` and ``stripe_subscription_id``
by the ``customer.subscription.created`` webhook.

The row's own UUID is handed to Stripe as ``site_lead_id`` in the
subscription metadata, and it is what keys the trial-started page. That
is deliberate: a raw ``sub_...`` id in a URL is guessable-adjacent and
would let anyone with one enumerate another buyer's trial. A UUID4 is
not enumerable, and the page it unlocks shows only the product name and
the trial end date.

## terms_version is stored, not derived

``settings.TERMS_VERSION`` is what the CURRENT buyer sees. This column
is what THIS buyer saw. They differ the moment the terms are revised,
and the difference is the whole point: a dispute in six months is about
the version live at signup, not the one live at trial end.

Deliberately NOT a FK to a ClientProfile or Account. Acceptance happens
on a public page, before any account exists, and sometimes for an email
that never converts. Email is the only identifier available at that
moment.
"""

from django.db import models

from core.models import TimestampedModel


class TermsAcceptance(TimestampedModel):
    """One buyer accepting one version of the terms for one product."""

    email = models.EmailField(db_index=True)
    product_slug = models.SlugField(
        max_length=100, db_index=True,
        help_text='ServiceTier.slug the acceptance was collected for.')
    terms_version = models.CharField(
        max_length=32,
        help_text='settings.TERMS_VERSION at the moment of acceptance.')
    accepted_at = models.DateTimeField(db_index=True)

    # Evidence of the acceptance itself. GenericIPAddressField rather
    # than CharField so an IPv6 client is stored correctly instead of
    # being silently truncated.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    # Kept even though this site uses Stripe Elements rather than
    # Stripe-hosted Checkout, so there is no Checkout Session to record
    # today. If the flow ever moves to hosted Checkout, the column is
    # already here and the webhook already knows to fill it.
    stripe_checkout_session_id = models.CharField(
        max_length=255, blank=True, db_index=True)
    # Filled by the customer.subscription.created webhook.
    stripe_customer_id = models.CharField(
        max_length=255, blank=True, db_index=True)
    stripe_subscription_id = models.CharField(
        max_length=255, blank=True, db_index=True)

    class Meta:
        ordering = ['-accepted_at']
        verbose_name = 'Terms acceptance'
        verbose_name_plural = 'Terms acceptances'
        indexes = [
            models.Index(fields=['email', 'product_slug']),
        ]

    def __str__(self):
        return (f'{self.email} accepted {self.terms_version} '
                f'for {self.product_slug} ({self.accepted_at:%Y-%m-%d %H:%M})')
