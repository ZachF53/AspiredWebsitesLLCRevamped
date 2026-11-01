"""
Processed-Stripe-event ledger, so redelivery is a no-op.

## Why this exists now and not before

Every handler in ``billing/webhooks.py`` was written to be individually
idempotent: get_or_create, update_or_create, "skip if already stamped".
That is the right design and it stays. It is also not sufficient for
what the trial work adds, because pushing a tag to GoHighLevel is not a
database write we can make idempotent on our side.

Stripe retries a webhook until it gets a 2xx, and sends the same event
more than once in normal operation. A redelivered
``customer.subscription.created`` would re-fire the
``trial-started-<product>`` tag, and in GHL a tag landing is what
triggers a workflow. The customer gets the welcome sequence twice.
``invoice.paid`` is worse: it is the event that converts a voice trial
into cohort occupancy, and double-counting it would move the price
ladder for the next real buyer.

So the ledger is keyed on Stripe's own event id and consulted before
dispatch. The side effects that cannot be undone get the protection;
the handlers keep their own idempotency as the second layer.

## Why the row is written BEFORE the handler runs

A handler that raises mid-way has, by definition, already done some of
its side effects. Marking the event processed first means a retry does
not repeat them. The cost is that a genuine transient failure is not
retried either, which is the right trade here: the webhook already
swallows handler exceptions and returns 200, so Stripe was never going
to retry those anyway. The failure is logged and `last_error` carries
it for a human.
"""

from django.db import models


class StripeWebhookEvent(models.Model):
    """One Stripe event id we have already accepted for processing."""

    event_id = models.CharField(
        max_length=255, unique=True, db_index=True,
        help_text="Stripe's event id, e.g. evt_1A2b3C. Unique: the "
                  "database constraint is what makes the check a race "
                  "rather than a read-then-write.")
    event_type = models.CharField(max_length=100, blank=True, db_index=True)
    processed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Filled when the handler raised. The event still counts as
    # processed (see module docstring), so this is the only trace that
    # something needs looking at.
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ['-processed_at']
        verbose_name = 'Stripe webhook event'
        verbose_name_plural = 'Stripe webhook events'

    def __str__(self):
        return f'{self.event_type or "?"} {self.event_id}'
