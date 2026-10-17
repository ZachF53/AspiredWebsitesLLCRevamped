"""
AI Voice Receptionist — the $97/mo standalone add-on.

## What this app is, and what it deliberately is not

GoHighLevel Voice AI is the engine, exactly as GHL workflows are the
engine for ``reviews/``. Each client has their own GHL sub-account with
its own phone number and its own Voice AI agent. GHL answers the call,
screens it, talks to the caller, transfers to a human, books the
appointment and sends the SMS. This app:

1. **Collects setup** — everything needed to build the client's Voice AI
   agent: hours, who answers first, transfer rules, what the agent knows.
   See the ``voice_receptionist`` wizard in ``onboarding/registry.py``.
2. **Holds the config** — the client edits it in the portal; we store it
   and flag the operator to push it to GHL.
3. **Meters** — ``VoiceCallRecord`` rows from the ``VoiceAiCallEnd``
   webhook, aggregated into ``VoiceUsagePeriod``.
4. **Gates access** — ``voice/entitlement.py``.
5. **Prices by cohort** — ``VoiceCohort`` + ``voice/cohorts.py``.

It does **not** place or answer calls, run speech recognition, or hold a
media stream. Nothing here should grow those. Beyond the architectural
boundary, there is a hard operational reason: Redis Cloud is capped at
``maxclients = 30`` and prod already sits around 26 (see the note in
``settings.py``). Realtime call handling would need a websocket per live
call and would take the whole site down under load. Webhook in, daily
beat sweep for reconciliation, nothing else.

## The one way this differs from reviews/

For ``reviews/`` the webhook feeds a stats page. Here it feeds **an
invoice**. A dropped webhook is lost revenue or an overcharge, so
metering is deliberately two-layer: the webhook accrues in real time (so
the 80%/100% warnings can fire during the month) and a nightly sweep over
GHL's call-logs API reconciles and is the authority. ``reconciled_at`` on
``VoiceUsagePeriod`` records which numbers have been through the second
pass.

## Retention — do not add a cleanup task

``VoiceCallRecord`` holds call transcripts and ``VoiceConsentAttestation``
is the record that the client was told about call recording. Both are
evidence if a recording-consent claim is ever made, and the call records
are the backing detail for every overage line item we have ever billed.
A well-meaning "tidy old rows" task destroys both the legal defence and
the billing audit trail. There is intentionally no purge command and no
retention setting.
"""

import math
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TimestampedModel


class VoiceCohort(TimestampedModel):
    """One rung of the price ladder, and the grandfathering ledger.

    The receptionist opens at $97 for 10 accounts, then steps +$50 per 10
    ($147, $197, $247...). Two things make that work without touching any
    display code:

    1. **Stripe already grandfathers.** A subscription stays on the Price
       object it was created with forever. Escalating is therefore just
       "mint a new Price and point new signups at it" — existing
       subscribers need no migration and no proration.
    2. **``ServiceTier`` mirrors the current rung.** Every public, portal
       and checkout surface reads ``tier.get_price_display`` and
       ``tier.stripe_price_id``, so repointing that one row is all it
       takes for the whole site to start quoting $147.

    This model is the history ``ServiceTier`` cannot hold: what each rung
    cost, which Stripe Price backs it, and which accounts are on it.

    Comped accounts DO consume a slot (owner's decision, Oct 2026). That
    is why ``filled`` counts entitled programs rather than paid
    subscriptions — and it is also why comping yourself and one free
    client makes the public "spots left" counter honestly read 8 of 10
    with no seed offset anywhere.
    """

    sequence = models.PositiveIntegerField(
        unique=True,
        help_text='1 is the opening cohort. Ascending, no gaps.')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stripe_price_id = models.CharField(max_length=100, blank=True)
    capacity = models.PositiveIntegerField(default=10)
    # Exactly one row should have is_current=True. Enforced by a partial
    # unique constraint below rather than by convention, because two
    # current cohorts would mean two prices being quoted at once and the
    # bug would surface as a customer dispute, not an exception.
    is_current = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['is_current'], condition=Q(is_current=True),
                name='voicecohort_one_current',
            ),
        ]

    def __str__(self):
        state = 'current' if self.is_current else 'closed'
        return f'Cohort {self.sequence} — ${self.price:,.0f} ({state})'

    @property
    def filled(self):
        """Accounts occupying a slot in this cohort."""
        return self.programs.count()

    @property
    def slots_remaining(self):
        return max(0, self.capacity - self.filled)

    @property
    def is_full(self):
        return self.slots_remaining == 0


class VoiceProgram(TimestampedModel):
    """One client's receptionist configuration and GHL link.

    Not an extension of ``MaintenancePlan``, for the same reasons
    ``ReviewProgram`` is not — most importantly that
    ``billing/account_provisioning._drop_websiteless_twin`` creates,
    re-keys and **deletes** plan rows to self-heal duplicates. The GHL
    credentials, the agent id and the whole call history must survive a
    cancel-and-resubscribe and must not be deletable by a dedup routine.

    It also has to outlive the subscription for a second reason reviews
    does not have: unbilled overage. If a client cancels mid-month owing
    40 minutes, the plan row goes to ``ended`` but the usage period still
    needs to be invoiced.
    """

    SETUP_CHOICES = [
        ('awaiting_onboarding', 'Awaiting onboarding'),
        ('awaiting_number', 'Awaiting number and agent build'),
        ('live', 'Live — answering'),
        ('suspended', 'Suspended'),
    ]

    # When the agent is allowed to answer. GHL's own model is "All Hours"
    # or a custom per-day schedule of ACTIVE windows, so after_hours_only
    # is expressed to GHL as the inverse of the client's business hours —
    # which is the whole reason BusinessHoursWindow exists even for a
    # client who only wants after-hours coverage.
    COVERAGE_CHOICES = [
        ('all_hours', 'All hours — 24/7'),
        ('after_hours_only', 'After hours only'),
        ('business_hours_only', 'Business hours only'),
        ('custom', 'Custom schedule'),
    ]

    # Who the caller reaches first.
    #
    # ai_first      — the agent answers every call. Spam screening works,
    #                 because the agent is what picks up.
    # ring_you_first— the client's phone rings for `ring_seconds`, then
    #                 the agent takes it. Spam screening is IMPOSSIBLE in
    #                 this mode: the robocall has already rung the
    #                 client's phone before the agent is ever involved.
    #                 That is a platform fact, not a packaging decision,
    #                 and the intake copy says so.
    ANSWER_CHOICES = [
        ('ai_first', 'AI answers first'),
        ('ring_you_first', 'Ring you first, AI picks up if you miss it'),
    ]

    TRANSFER_FAIL_CHOICES = [
        ('voicemail', 'Take a voicemail'),
        ('ai_takes_message', 'AI takes the message itself'),
        ('sms_followup', 'Text the caller a follow-up'),
    ]

    NUMBER_STRATEGY_CHOICES = [
        ('forward_existing', 'Forward their existing number to us'),
        ('new_number', 'Give them a new number'),
        ('port_existing', 'Port their existing number to us'),
    ]

    account = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='voice_programs')
    # Nullable for the same reason ReviewProgram.website is: the default
    # is one receptionist per account, but a two-brand client needs two
    # agents with two greetings. Don't force it; don't foreclose it.
    website = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='voice_programs')

    cohort = models.ForeignKey(
        VoiceCohort, on_delete=models.PROTECT, null=True, blank=True,
        related_name='programs')
    # Snapshotted from the cohort at grant time. The cohort's own price
    # is authoritative for NEW signups; this is what THIS client pays,
    # and it must not move when the cohort row is edited.
    locked_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True)

    # ── Business identity ────────────────────────────────────────────
    legal_business_name = models.CharField(max_length=200, blank=True)
    dba_name = models.CharField(max_length=200, blank=True)
    business_street = models.CharField(max_length=200, blank=True)
    business_city = models.CharField(max_length=100, blank=True)
    business_state = models.CharField(max_length=50, blank=True)
    business_zip = models.CharField(max_length=10, blank=True)
    business_phone = models.CharField(max_length=20, blank=True)
    business_website_url = models.URLField(blank=True)
    support_email = models.EmailField(blank=True)
    # Required in practice, not just conventional: GHL's Voice AI working
    # hours have no timezone field of their own, so the schedule we push
    # is only correct if we know what zone the client's clock is in.
    # Default is Central because the client base is Texas-first.
    timezone = models.CharField(max_length=64, default='America/Chicago')

    # ── GHL link ─────────────────────────────────────────────────────
    ghl_location_id = models.CharField(
        max_length=64, blank=True, db_index=True)
    ghl_agent_id = models.CharField(max_length=64, blank=True)
    ghl_token_encrypted = models.TextField(blank=True)
    ghl_token_hint = models.CharField(max_length=12, blank=True)
    ghl_token_set_at = models.DateTimeField(null=True, blank=True)
    ghl_token_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name='+')
    answering_number = models.CharField(max_length=20, blank=True)
    number_strategy = models.CharField(
        max_length=20, choices=NUMBER_STRATEGY_CHOICES, blank=True)

    # ── Agent configuration (mirrors what we push to GHL) ────────────
    coverage_mode = models.CharField(
        max_length=24, choices=COVERAGE_CHOICES, default='all_hours')
    answer_mode = models.CharField(
        max_length=20, choices=ANSWER_CHOICES, default='ai_first')
    # A TARGET, not a setting we control.
    #
    # Calls reach the agent by conditional call-forwarding from the
    # client's existing line, and the no-answer timer that decides when
    # a call forwards lives at their CARRIER. Some carriers let the
    # timer be set (GSM `*61*<number>**<seconds>#`), some fix it around
    # 20-25s, some will not expose it at all. We only control it exactly
    # when the number has been ported to us (number_strategy='port_
    # existing'), because then there is no upstream carrier in the path.
    #
    # Stored anyway: it is what we ask the carrier for, what we tell the
    # client to expect, and the real value once a number is ported.
    # Do not write copy that presents it as a guarantee — see the intake
    # help text in onboarding/registry.py and
    # voice/tests_ring_seconds.py, which pins that honesty.
    ring_seconds = models.PositiveIntegerField(
        default=20,
        help_text='Target ring time before the agent picks up. Only '
                  'meaningful when answer_mode=ring_you_first, and only '
                  'exact when the number is ported to us — otherwise the '
                  "carrier's own no-answer timer governs.")
    transfer_number = models.CharField(max_length=20, blank=True)
    transfer_conditions = models.TextField(
        blank=True,
        help_text='Natural language — GHL takes prose trigger conditions.')
    after_transfer_fail = models.CharField(
        max_length=20, choices=TRANSFER_FAIL_CHOICES, default='voicemail')
    greeting = models.TextField(blank=True)
    services_offered = models.TextField(blank=True)
    service_area = models.TextField(blank=True)
    faq_text = models.TextField(blank=True)
    emergency_handling = models.TextField(blank=True)
    collect_fields = models.JSONField(default=list, blank=True)
    max_call_minutes = models.PositiveIntegerField(default=10)
    voice_choice = models.CharField(max_length=60, blank=True)
    booking_enabled = models.BooleanField(default=False)
    recording_enabled = models.BooleanField(default=True)

    # False means the operator owes GHL a push. Same pattern as
    # ReviewProgram.wording_synced_to_ghl.
    config_synced_to_ghl = models.BooleanField(default=True)
    config_updated_at = models.DateTimeField(null=True, blank=True)

    # ── State ────────────────────────────────────────────────────────
    setup_status = models.CharField(
        max_length=24, choices=SETUP_CHOICES,
        default='awaiting_onboarding', db_index=True)
    paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_by_client = models.BooleanField(default=False)

    # Public, unguessable, and deliberately NOT a secret — it identifies
    # a program in the status endpoint the GHL agent polls. Separate from
    # any secret so a public URL can never be turned into a privileged
    # one. Same reasoning as ReviewProgram.public_key.
    public_key = models.UUIDField(
        default=uuid.uuid4, unique=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # unique_together on a nullable FK does NOT do what you want:
            # SQL treats NULLs as distinct, so two account-level rows
            # would both be legal. Two partial constraints instead.
            models.UniqueConstraint(
                fields=['account'], condition=Q(website__isnull=True),
                name='voiceprogram_one_per_account',
            ),
            models.UniqueConstraint(
                fields=['account', 'website'],
                condition=Q(website__isnull=False),
                name='voiceprogram_one_per_website',
            ),
        ]

    def __str__(self):
        return f'{self.display_name} — {self.get_setup_status_display()}'

    @property
    def display_name(self):
        return (self.dba_name or self.legal_business_name
                or self.account.name or 'Unnamed')

    @property
    def is_live(self):
        return self.setup_status == 'live' and not self.paused

    @property
    def screens_spam(self):
        """Spam screening requires the agent to answer first.

        In ring_you_first the robocall rings the client's phone before
        the agent exists, so there is nothing to screen. This is a
        platform constraint; every surface that advertises screening
        must read it from here rather than assuming it is always on.
        """
        return self.answer_mode == 'ai_first'

    def ghl_token(self):
        from vault.crypto import server_decrypt
        if not self.ghl_token_encrypted:
            return ''
        return server_decrypt(self.ghl_token_encrypted)

    def blocking_reasons(self):
        """Why this program cannot go live yet. Operator-facing."""
        reasons = []
        if not self.ghl_location_id:
            reasons.append('No GHL location id — paste the sub-account id.')
        if not self.ghl_token_encrypted:
            reasons.append('No GHL token — paste the Private Integration '
                           'Token.')
        if not self.ghl_agent_id:
            reasons.append('No Voice AI agent id — build the agent in GHL '
                           'and paste its id.')
        if not self.answering_number:
            reasons.append('No answering number.')
        if not self.greeting:
            reasons.append('No greeting — the client has not finished '
                           'setup.')
        if (self.answer_mode == 'ring_you_first'
                and not self.transfer_number):
            reasons.append('Ring-you-first is selected but there is no '
                           'number to ring.')
        if self.coverage_mode != 'all_hours' and not self.hours.exists():
            reasons.append('Coverage is not 24/7 but no business hours '
                           'are set, so the schedule cannot be computed.')
        if self.recording_enabled and not self.attestations.exists():
            reasons.append('Call recording is on but the client has not '
                           'signed the recording disclosure.')
        return reasons


class BusinessHoursWindow(TimestampedModel):
    """One open window in a client's week.

    Schema shape copied from ``scheduler.AvailabilityWindow`` — that one
    is the agency's own booking availability, this is the client's trading
    hours, but the fields and the day encoding are identical and there is
    no reason to invent a second convention.

    Stored even when ``coverage_mode='after_hours_only'``, because
    after-hours is computed as the inverse of these windows. A client who
    only wants overnight cover still has to tell us when they close.
    """

    DAY_CHOICES = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
        (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]

    program = models.ForeignKey(
        VoiceProgram, on_delete=models.CASCADE, related_name='hours')
    day_of_week = models.PositiveSmallIntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ['day_of_week', 'start_time']

    def __str__(self):
        return (f'{self.get_day_of_week_display()} '
                f'{self.start_time:%H:%M}–{self.end_time:%H:%M}')

    @property
    def crosses_midnight(self):
        """GHL supports windows that run past midnight; flag them so the
        schedule builder can split them rather than emit end < start."""
        return self.end_time <= self.start_time


class VoiceCallRecord(TimestampedModel):
    """One completed call, from the ``VoiceAiCallEnd`` webhook.

    Three fields carry more weight than they look like they do:

    * ``duration_seconds`` — GHL sends SECONDS. Stored as sent and
      converted only at the billing edge, so no rounding decision is
      baked into the raw record.
    * ``trial_call`` — GHL marks test calls. These are stored (the
      operator wants to see their own test calls) but MUST NOT bill, or
      building a client's agent charges the client for it.
    * ``transferred`` — derived from ``executedCallActions`` containing
      ``CALL_TRANSFER``. GHL has no outcome or status field, so this is
      the only way to know a human took over.
    """

    program = models.ForeignKey(
        VoiceProgram, on_delete=models.CASCADE, related_name='calls',
        null=True, blank=True)
    # unique=True with null=True, never blank-string-unique: a second
    # blank string would collide and reject a legitimate call. Same
    # reasoning as ReviewEvent.dedupe_key.
    ghl_call_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True)
    ghl_location_id = models.CharField(
        max_length=64, blank=True, db_index=True)
    ghl_agent_id = models.CharField(max_length=64, blank=True)

    started_at = models.DateTimeField(db_index=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    from_number = models.CharField(max_length=20, blank=True)
    trial_call = models.BooleanField(default=False, db_index=True)

    transferred = models.BooleanField(default=False)
    booked_appointment = models.BooleanField(default=False)
    spam_screened = models.BooleanField(default=False)
    after_hours = models.BooleanField(default=False)

    summary = models.TextField(blank=True)
    transcript = models.TextField(blank=True)
    extracted_data = models.JSONField(default=dict, blank=True)
    executed_actions = models.JSONField(default=list, blank=True)
    # Raw payload stored before interpretation, so a mapping bug can be
    # replayed rather than re-collected.
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['program', 'started_at'],
                         name='voicecall_prog_at'),
            models.Index(fields=['program', 'trial_call', 'started_at'],
                         name='voicecall_prog_billable'),
        ]

    def __str__(self):
        return f'{self.from_number or "unknown"} — {self.duration_seconds}s'

    @property
    def masked_number(self):
        digits = ''.join(c for c in self.from_number if c.isdigit())
        if len(digits) < 10:
            return self.from_number or ''
        return f'({digits[-10:-7]}) •••-{digits[-4:]}'

    @property
    def is_billable(self):
        return not self.trial_call


class VoiceUsagePeriod(TimestampedModel):
    """Minutes for one billing period, and the overage charged for them.

    Rounding happens here and nowhere else: billable SECONDS are summed
    across the whole period, then converted once. Rounding each call up
    to a minute instead would inflate a 40-call month by roughly 20
    minutes of phantom usage, which is a refund conversation waiting to
    happen.
    """

    program = models.ForeignKey(
        VoiceProgram, on_delete=models.CASCADE, related_name='usage_periods')
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField(db_index=True)

    included_minutes = models.PositiveIntegerField(default=300)
    billable_seconds = models.PositiveIntegerField(default=0)
    overage_minutes = models.PositiveIntegerField(default=0)
    overage_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'))
    overage_rate = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal('0.2500'))

    stripe_invoice_item_id = models.CharField(max_length=80, blank=True)
    stripe_invoice_id = models.CharField(max_length=80, blank=True)
    billed_at = models.DateTimeField(null=True, blank=True)

    # Set when this period's overage will never be charged — today that
    # means the client is comped. Comping the receptionist has to comp
    # the minutes too: an operator who grants the product free and then
    # invoices $12 of overage has broken the deal they offered.
    #
    # A real column rather than just skipping the charge, for two
    # reasons. The sweep looks for periods with overage and no invoice
    # item, so a silent skip would re-examine the same rows every night
    # forever. And "we chose not to bill this" is different from "this
    # has not been billed yet" — the admin usage table shows which.
    billing_waived = models.BooleanField(default=False)
    billing_waived_reason = models.CharField(max_length=40, blank=True)

    # Idempotency for the threshold warnings — set once, checked before
    # sending, so a burst of calls crossing 80% cannot send five emails.
    warned_80_at = models.DateTimeField(null=True, blank=True)
    warned_100_at = models.DateTimeField(null=True, blank=True)

    # Stamped by the nightly sweep. A period that has never been
    # reconciled is webhook-only and should not be invoiced.
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-period_start']
        constraints = [
            models.UniqueConstraint(
                fields=['program', 'period_start'],
                name='voiceusage_one_per_period',
            ),
        ]
        indexes = [
            models.Index(fields=['program', 'period_start'],
                         name='voiceusage_prog_start'),
        ]

    def __str__(self):
        return (f'{self.program_id} {self.period_start:%Y-%m-%d} — '
                f'{self.minutes_used}/{self.included_minutes} min')

    @property
    def minutes_used(self):
        """Whole minutes consumed, rounded up at the period level."""
        if not self.billable_seconds:
            return 0
        return math.ceil(self.billable_seconds / 60)

    @property
    def minutes_remaining(self):
        return max(0, self.included_minutes - self.minutes_used)

    @property
    def percent_used(self):
        if not self.included_minutes:
            return 0
        return int(round(100 * self.minutes_used / self.included_minutes))

    @property
    def is_over(self):
        return self.minutes_used > self.included_minutes


class VoiceConsentAttestation(TimestampedModel):
    """The client's acknowledgement that calls are recorded.

    Append-only. Never updated, never deleted — it is an indemnity, not a
    preference, and the same reasoning as
    ``ReviewConsentAttestation`` applies.

    Why a voice product needs one at all: the clients are in Texas and
    Georgia, both one-party-consent states, but their callers are not
    necessarily. A call from California, Florida, Illinois, Pennsylvania
    or Washington lands in two-party-consent territory. The mitigation is
    a recorded disclosure at the top of the call, which the greeting
    carries; this row is the record that the client was told that is
    their obligation and agreed to it.
    """

    program = models.ForeignKey(
        VoiceProgram, on_delete=models.CASCADE, related_name='attestations')
    onboarding = models.ForeignKey(
        'onboarding.Onboarding', on_delete=models.SET_NULL, null=True,
        blank=True, related_name='+')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name='+')

    version = models.CharField(max_length=20)
    # A snapshot, not a reference. If the wording changes, an old
    # attestation must still say what the client actually agreed to.
    attestation_text = models.TextField()
    answers = models.JSONField(default=dict, blank=True)
    typed_name = models.CharField(max_length=200)
    typed_title = models.CharField(max_length=120, blank=True)
    agreed_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    class Meta:
        ordering = ['-agreed_at']

    def __str__(self):
        return f'{self.typed_name} — {self.agreed_at:%Y-%m-%d}'
