"""
Review System — the $97/mo standalone product (and the thing bundled into
the Full Plan).

## What this app is, and what it deliberately is not

GoHighLevel is the engine. Each client has their own GHL sub-account with
its own A2P 10DLC brand (registered against their EIN) and its own phone
number. Completed-job events go straight into GHL via Zapier or a native
integration; **GHL workflows** send the SMS and the email, fire the day-3
reminder, raise the internal SMS on a negative reply, and notify the
client. Carrier-level STOP/DND is GHL's too.

So this app does exactly four things:

1. **Collects setup** — everything needed to submit the A2P brand and
   campaign and to build the client's GHL workflow. See the
   ``review_system`` onboarding wizard in ``onboarding/registry.py``.
2. **Holds the approved wording** — the client edits it in the portal
   behind compliance guardrails (``reviews/templating.py``); we validate
   and store it and flag the operator to paste it into GHL.
3. **Reports** — ``ReviewEvent`` rows posted back by the GHL workflow.
4. **Gates access** — ``reviews/entitlement.py``.

It does **not** receive job events, schedule sends, send SMS, handle
STOP, or maintain a suppression list. Nothing here should grow those: if
a future change needs them, the GHL boundary has moved and that is a
design decision, not an implementation detail.

## Retention — do not add a cleanup task

``ReviewProgram``, ``ReviewConsentAttestation`` and ``ReviewEvent`` are
the evidence in a TCPA claim. The statute of limitations is four years;
keep at least five. A well-meaning "tidy old rows" task over any of
these tables destroys the defence. There is intentionally no purge
management command and no retention setting.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TimestampedModel


class ReviewProgram(TimestampedModel):
    """One client's review-request configuration.

    Not an extension of ``MaintenancePlan``, for three reasons:

    1. A Full Plan client has **no** plan row carrying review
       entitlement — reviews are bundled — so there would be nothing to
       hang the config off.
    2. ``billing/account_provisioning._drop_websiteless_twin`` creates,
       re-keys and **deletes** plan rows to self-heal duplicates. The GHL
       token and the A2P brand must survive a cancel-and-resubscribe and
       must not be deletable by a dedup routine.
    3. ``MaintenancePlan.TIER_CHOICES`` and ``current_monthly_price`` are
       maintenance-specific; the review tier is ``category='addon'``.
    """

    SETUP_CHOICES = [
        ('awaiting_onboarding', 'Awaiting onboarding'),
        ('awaiting_a2p', 'Awaiting carrier registration'),
        ('live', 'Live — sending'),
        ('suspended', 'Suspended'),
    ]
    A2P_CHOICES = [
        ('not_started', 'Not started'),
        ('submitted', 'Submitted'),
        ('pending', 'Pending carrier review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    ENTITY_CHOICES = [
        ('sole_prop', 'Sole proprietor'),
        ('llc', 'LLC'),
        ('s_corp', 'S-Corporation'),
        ('c_corp', 'C-Corporation'),
        ('partnership', 'Partnership'),
        ('non_profit', 'Non-profit'),
    ]

    account = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='review_programs',
    )
    # Nullable because the default is one program per account. The FK
    # exists because an Account can own several Websites with different
    # Google listings, and a two-brand client needs two review links.
    # Don't force it; don't foreclose it.
    website = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='review_programs',
    )

    # ── Identity, for the A2P brand registration ──
    # Brand rejection cause #1 is a legal-name/EIN mismatch against IRS
    # records, which is why the wizard insists on the exact name.
    legal_business_name = models.CharField(max_length=200, blank=True)
    dba_name = models.CharField(max_length=200, blank=True)
    entity_type = models.CharField(
        max_length=20, choices=ENTITY_CHOICES, blank=True)
    # Server-key AES-256-GCM (vault.crypto.server_encrypt). Never
    # rendered on page load; revealing it writes an audit alert.
    ein_encrypted = models.TextField(blank=True)
    ein_last4 = models.CharField(max_length=4, blank=True)
    business_street = models.CharField(max_length=200, blank=True)
    business_city = models.CharField(max_length=100, blank=True)
    business_state = models.CharField(max_length=50, blank=True)
    business_zip = models.CharField(max_length=10, blank=True)
    business_phone = models.CharField(max_length=20, blank=True)
    business_website_url = models.URLField(blank=True)
    support_email = models.EmailField(blank=True)
    timezone = models.CharField(max_length=64, default='America/Chicago')

    # ── Google ──
    google_review_url = models.URLField(blank=True)
    google_business_name = models.CharField(max_length=200, blank=True)

    # ── GHL sub-account (operator pastes these in) ──
    # ghl_location_id resolves inbound webhooks to a program, so it is
    # indexed and effectively required once live. The token is optional
    # in v1 — nothing calls GHL outbound yet — but storing it now means a
    # later pull (reviews, contact lookup) needs no migration.
    ghl_location_id = models.CharField(max_length=64, blank=True, db_index=True)
    ghl_token_encrypted = models.TextField(blank=True)
    ghl_token_hint = models.CharField(max_length=12, blank=True)
    ghl_token_set_at = models.DateTimeField(null=True, blank=True)
    ghl_token_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    sending_number = models.CharField(max_length=20, blank=True)

    # ── A2P status: we track it, we never provision it ──
    a2p_brand_status = models.CharField(
        max_length=20, choices=A2P_CHOICES, default='not_started')
    a2p_campaign_status = models.CharField(
        max_length=20, choices=A2P_CHOICES, default='not_started')
    a2p_submitted_at = models.DateTimeField(null=True, blank=True)
    a2p_approved_at = models.DateTimeField(null=True, blank=True)
    a2p_rejection_reason = models.TextField(blank=True)

    setup_status = models.CharField(
        max_length=24, choices=SETUP_CHOICES,
        default='awaiting_onboarding', db_index=True,
    )

    # ── Sending controls ──
    # `sending_paused` is only meaningful because the client's GHL
    # workflow calls /api/reviews/status/<public_key>/ as its first step
    # and exits when it reports paused. Without that workflow step this
    # flag is decorative — see the GHL build checklist.
    sending_paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_by_client = models.BooleanField(default=False)
    reminder_enabled = models.BooleanField(default=True)
    send_delay = models.CharField(max_length=20, blank=True)

    # ── Client-approved wording (guardrailed by reviews.templating) ──
    sms_body_template = models.TextField(blank=True)
    email_subject_template = models.CharField(max_length=200, blank=True)
    email_body_template = models.TextField(blank=True)
    # False means the portal copy is ahead of what the GHL workflow
    # actually sends, i.e. the operator owes a paste.
    wording_synced_to_ghl = models.BooleanField(default=True)
    wording_updated_at = models.DateTimeField(null=True, blank=True)

    # Public, unguessable, and deliberately NOT a secret: it keys the
    # status endpoint the GHL workflow polls. Separate from any secret so
    # a public URL can never be turned into a privileged one.
    public_key = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # unique_together on a nullable FK does NOT do what you want:
            # SQL treats NULLs as distinct, so two account-level rows
            # would both be legal. Two partial constraints instead.
            models.UniqueConstraint(
                fields=['account'], condition=Q(website__isnull=True),
                name='reviewprogram_one_per_account',
            ),
            models.UniqueConstraint(
                fields=['account', 'website'],
                condition=Q(website__isnull=False),
                name='reviewprogram_one_per_website',
            ),
        ]

    def __str__(self):
        return f'Review System — {self.display_name}'

    @property
    def display_name(self):
        return (self.legal_business_name or self.google_business_name
                or getattr(self.account, 'name', '') or 'Unnamed')

    @property
    def is_live(self):
        return self.setup_status == 'live' and not self.sending_paused

    def ein(self):
        """Decrypted EIN, or ''. Every call site must be audited."""
        from vault.crypto import server_decrypt
        return server_decrypt(self.ein_encrypted)

    def ghl_token(self):
        """Decrypted GHL token, or ''."""
        from vault.crypto import server_decrypt
        return server_decrypt(self.ghl_token_encrypted)

    def blocking_reasons(self):
        """Why this program cannot go live yet, as a list of sentences.

        Empty list means "Mark live" is allowed. A program must never go
        live without a consent attestation on file — that record is the
        only thing standing between us and an unanswerable TCPA claim.
        """
        reasons = []
        if not self.ghl_location_id:
            reasons.append('No GHL location ID — paste it in Connection.')
        if not self.sending_number:
            reasons.append('No sending number recorded.')
        if self.a2p_brand_status != 'approved':
            reasons.append('A2P brand is not approved yet.')
        if self.a2p_campaign_status != 'approved':
            reasons.append('A2P campaign is not approved yet.')
        if not self.google_review_url:
            reasons.append('No Google review link — collected in onboarding.')
        if not self.attestations.exists():
            reasons.append(
                'No consent attestation on file. The client must complete '
                'the onboarding wizard themselves.')
        return reasons


class ReviewConsentAttestation(TimestampedModel):
    """The client's signed warranty that they may text their customers.

    **Append-only. Never updated, never deleted.** This is the exhibit in
    a TCPA claim: it records that the client told us, in writing, with
    identity evidence, that their work order discloses service texts and
    that they hold the numbers lawfully.

    It is an indemnity, not consent. The actual consent lives on the
    client's work order, which we never see. This record only shifts
    liability if the contract says so too — see the indemnification
    clause requirement in the plan.
    """

    program = models.ForeignKey(
        ReviewProgram, on_delete=models.CASCADE, related_name='attestations')
    onboarding = models.ForeignKey(
        'onboarding.Onboarding', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    version = models.CharField(max_length=20)
    # A snapshot, not a reference. The wording WILL change, and the
    # defence needs what *this* client actually saw and agreed to.
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


class ReviewEvent(TimestampedModel):
    """Something the client's GHL workflow reported back.

    One table rather than separate sent/reply/click/review models: every
    row has the same shape (a program, a type, a timestamp, a customer,
    a raw payload), the dashboard groups them by ``event_type``, and
    splitting them would quadruple the webhook's write path for no new
    fact.

    ``review_rating`` and ``review_text`` describe a review that is
    **already public on Google**. Neither is ever an input to who gets
    asked — there is deliberately no pre-send rating, sentiment or
    satisfaction field anywhere in this app, because a "send only to
    happy customers" feature would then require a migration. That is the
    enforcement of the no-gating promise on the public service page.
    """

    EVENT_CHOICES = [
        ('sent', 'Request sent'),
        ('delivered', 'Delivered'),
        ('reply', 'Customer replied'),
        ('click', 'Review link clicked'),
        ('review_left', 'Review left'),
        ('opt_out', 'Opted out'),
        ('failed', 'Send failed'),
    ]

    program = models.ForeignKey(
        ReviewProgram, on_delete=models.CASCADE, related_name='events',
        null=True, blank=True,
    )
    event_type = models.CharField(
        max_length=16, choices=EVENT_CHOICES, db_index=True)
    # Stored before interpretation, exactly as outreach.InstantlyEvent
    # does it, so a misclassification is diagnosable and replayable.
    payload = models.JSONField(default=dict, blank=True)
    # unique=True with null=True, never blank-string-unique: a second
    # blank string would collide and reject a legitimate event.
    dedupe_key = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True)
    ghl_location_id = models.CharField(max_length=64, blank=True, db_index=True)
    occurred_at = models.DateTimeField(db_index=True)

    customer_name = models.CharField(max_length=200, blank=True)
    customer_phone_e164 = models.CharField(max_length=20, blank=True)
    customer_email = models.EmailField(blank=True)

    body = models.TextField(blank=True)
    review_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    review_text = models.TextField(blank=True)

    # A reply we could not classify as STOP/HELP — somebody has to read
    # it. GHL separately fires the operator SMS and the client
    # notification; this is the durable record and the portal's list.
    needs_human = models.BooleanField(default=False, db_index=True)
    processed = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            # Drives every dashboard aggregate (monthly counts, the
            # 30/60/90/YTD tiles) and the per-type log filters.
            models.Index(fields=['program', 'event_type', 'occurred_at'],
                         name='reviewevent_prog_type_at'),
            models.Index(fields=['program', 'needs_human'],
                         name='reviewevent_prog_needshuman'),
        ]

    def __str__(self):
        return f'{self.get_event_type_display()} — {self.occurred_at:%Y-%m-%d}'

    @property
    def masked_phone(self):
        """``(210) •••-0134`` — enough for the client to recognise the
        customer, not enough to be a bulk phone-list export."""
        digits = ''.join(c for c in self.customer_phone_e164 if c.isdigit())
        if len(digits) < 10:
            return self.customer_phone_e164 or ''
        area, last4 = digits[-10:-7], digits[-4:]
        return f'({area}) •••-{last4}'
