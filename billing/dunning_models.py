"""
DunningEvent — the ledger of which payment-failure stages have already
run for a given failure window.

This model exists to replace scheduling. The old design queued nine
`apply_async(countdown=...)` messages the moment a payment failed, which
meant the decision to act was made days or weeks before the action ran,
against state that could no longer be true. On 2026-09-02 that put 42
copies of a "your payment failed" email in front of a client who had
paid a week earlier.

The sweep re-derives what is due every day from current state. This table
is what stops it re-running a stage it already ran, and the uniqueness is
enforced by the DATABASE rather than by a cache key or a task id — so it
holds across concurrent workers, restarts, and a Redis outage.

`window_started_at` carries the `Account.payment_failure_started_at`
value the row belongs to. That is what scopes the ledger to one failure
window: when a client is reinstated the guard is nulled, and a later
genuine failure stamps a fresh timestamp, so its stages claim cleanly
without anything needing to delete old rows.
"""

from django.db import models

from core.models import TimestampedModel


class DunningEvent(TimestampedModel):
    # Stage identifiers. The day thresholds live in billing/dunning.py
    # next to the handlers, so the schedule is readable in one place.
    STAGE_EMAIL_3 = 'email_3'
    STAGE_EMAIL_7 = 'email_7'
    STAGE_EMAIL_14 = 'email_14'
    STAGE_MAINTENANCE = 'maintenance_14'
    STAGE_OFFLINE = 'offline_21'
    STAGE_DESTROY = 'destroy_30'
    STAGE_SNAPSHOT_DELETE = 'snapshot_delete_60'

    STAGE_CHOICES = [
        (STAGE_EMAIL_3, 'Day 3 — payment failed email'),
        (STAGE_EMAIL_7, 'Day 7 — payment failed email'),
        (STAGE_EMAIL_14, 'Day 14 — payment failed email'),
        (STAGE_MAINTENANCE, 'Day 14 — site to maintenance page'),
        (STAGE_OFFLINE, 'Day 21 — droplet powered off'),
        (STAGE_DESTROY, 'Day 30 — droplet destroyed'),
        (STAGE_SNAPSHOT_DELETE, 'Day 60 — snapshot deleted'),
    ]

    # `done`               — the action ran.
    # `awaiting_approval`  — the stage came due but is destructive, so the
    #                        sweep alerted an operator and stopped. Claimed
    #                        so the sweep does not re-alert every day.
    # `approved`           — an operator confirmed; the action then ran.
    # `cancelled`          — the window closed before approval; never ran.
    # `failed`             — the action raised. Claimed either way, so a
    #                        broken droplet call cannot spin daily.
    STATUS_DONE = 'done'
    STATUS_AWAITING_APPROVAL = 'awaiting_approval'
    STATUS_APPROVED = 'approved'
    STATUS_CANCELLED = 'cancelled'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_DONE, 'Done'),
        (STATUS_AWAITING_APPROVAL, 'Awaiting approval'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_FAILED, 'Failed'),
    ]

    account = models.ForeignKey(
        'clients.Account',
        on_delete=models.CASCADE,
        related_name='dunning_events',
    )
    # Null for the account-level email stages; set for the per-site
    # droplet stages. See the two Meta constraints below — a nullable
    # column in a unique_together would let duplicate email stages
    # through, because SQL treats NULLs as distinct from each other.
    website = models.ForeignKey(
        'clients.Website',
        on_delete=models.CASCADE,
        related_name='dunning_events',
        null=True,
        blank=True,
    )
    window_started_at = models.DateTimeField()
    stage = models.CharField(max_length=32, choices=STAGE_CHOICES)
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_DONE)
    detail = models.TextField(blank=True)

    approved_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_dunning_events',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # Split in two because `website` is nullable. A single
            # unique_together across a nullable column enforces nothing
            # on the NULL rows — every account-level email stage would
            # be free to insert again, which is precisely the duplicate
            # this table exists to prevent.
            models.UniqueConstraint(
                fields=['account', 'window_started_at', 'stage'],
                condition=models.Q(website__isnull=True),
                name='uniq_dunning_account_stage_per_window',
            ),
            models.UniqueConstraint(
                fields=['account', 'website', 'window_started_at', 'stage'],
                condition=models.Q(website__isnull=False),
                name='uniq_dunning_site_stage_per_window',
            ),
        ]
        indexes = [
            models.Index(fields=['account', 'window_started_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.account_id} {self.stage} ({self.status})'
