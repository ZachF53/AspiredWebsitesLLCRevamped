"""
StripeRevenueSync — singleton holding the live-Stripe-sourced "money
collected this year" figure shown on the v2 dashboard money card.

Why this exists separately from PaymentRecord: PaymentRecord is our own
ledger, written only when our own billing flows run (webhooks, or
backfill_payment_ledger for a known Account.stripe_customer_id). A
payment collected outside that — e.g. a client billed by hand directly
in the Stripe dashboard with no Account link at all — never lands
there. This table is populated by a Celery beat sweep
(billing.tasks.sync_stripe_ytd_collected_task, every 5 minutes) that
pulls every succeeded charge on the account straight from Stripe,
unscoped by customer, so nothing manually charged gets missed.

`year` doubles as the reset marker — the sweep detects a year rollover
by comparing against `timezone.now().year` and starts summing fresh
from Jan 1 rather than needing a separate cron job to zero it out.
"""

import uuid

from django.db import models

from core.models import TimestampedModel

# Distinct from vault.VaultConfig's SINGLETON_ID (int=1) — different
# table, so no actual collision risk, but kept distinct for clarity.
SINGLETON_ID = uuid.UUID(int=2)


class StripeRevenueSync(TimestampedModel):
    year = models.IntegerField(default=0)
    total_collected = models.DecimalField(
        max_digits=12, decimal_places=2, default=0)
    charge_count = models.IntegerField(default=0)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Stripe Revenue Sync'
        verbose_name_plural = 'Stripe Revenue Sync'

    def save(self, *args, **kwargs):
        self.pk = SINGLETON_ID
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=SINGLETON_ID)
        return obj

    def __str__(self):
        return f'Stripe YTD collected ({self.year}): ${self.total_collected}'
