from django.db import models
from django.utils import timezone


class Lead(models.Model):
    """
    Canonical Lead model for ALL sources:
    scraped (Google Maps, State Bar), inbound (contact form, audit tool),
    or manual entry. See CLAUDE.md → Data Model Decisions.
    """

    SOURCE_CHOICES = [
        ('google_maps', 'Google Maps'),
        ('state_bar', 'State Bar Directory'),
        ('contact_form', 'Contact Form'),
        ('audit_tool', 'Audit Tool'),
        ('manual', 'Manual Entry'),
        ('counsel_south', 'Counsel South'),
        # Apify contact database — the only source that arrives WITH an
        # email address rather than needing one scraped off a homepage.
        ('apify', 'Apify'),
    ]

    STATUS_CHOICES = [
        ('new', 'New'),
        ('contacted', 'Contacted'),
        ('replied', 'Replied'),
        ('call_booked', 'Call Booked'),
        ('proposal_sent', 'Proposal Sent'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        ('unsubscribed', 'Unsubscribed'),
        ('archived', 'Archived'),
    ]

    TEMPERATURE_CHOICES = [
        ('hot', 'Hot'),
        ('warm', 'Warm'),
        ('cold', 'Cold'),
    ]

    # ── Inbound sources never receive cold outreach ────────────────────
    #
    # These people contacted US. Sending them a cold sequence that opens
    # "I've been reaching out to law firms in Houston and yours caught my
    # eye" to somebody who filled in the contact form last Tuesday reads
    # as though nobody read their message, and it is the kind of mistake
    # a prospect tells other people about.
    #
    # They get a human reply instead: the contact form already fires an
    # auto-acknowledgement and an internal notification the moment it is
    # submitted.
    #
    # Nothing enforced this before. An inbound lead cleared verification,
    # the segment gate and the icebreaker guard identically to a scraped
    # one; the only thing standing between a contact-form submission and
    # a cold email was that campaign assignment had not been built yet.
    # That is luck, not a safeguard.
    INBOUND_SOURCES = frozenset({'contact_form', 'audit_tool'})

    @property
    def is_inbound(self):
        """True when this person contacted us rather than us finding them."""
        return self.source in self.INBOUND_SOURCES

    # Business info
    firm_name = models.CharField(max_length=255)
    attorney_name = models.CharField(max_length=255, blank=True)
    practice_area = models.CharField(max_length=100, blank=True)
    business_type = models.CharField(max_length=100, default='Law Firm')

    # Contact info
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)

    # Google presence
    google_rating = models.DecimalField(
        max_digits=3, decimal_places=1, null=True, blank=True
    )
    google_review_count = models.IntegerField(default=0)
    has_google_business = models.BooleanField(default=False)

    # When we last asked Google Places about this firm — see
    # outreach/google_profile.py.
    #
    # Stamped on a MISS as well as a hit, which is the whole point: a firm
    # with no Places listing would otherwise be re-queried on every run
    # forever, paying per lookup to rediscover the same nothing.
    google_profile_checked_at = models.DateTimeField(null=True, blank=True)
    # Why a lookup did not produce a rating. Kept because "no rating" has
    # several causes with different fixes — unlisted, name too generic to
    # match safely, or a listing with no reviews yet — and a bare null
    # cannot tell them apart.
    google_profile_note = models.CharField(max_length=200, blank=True)

    # Website audit results (from PageSpeed)
    website_performance_score = models.IntegerField(null=True, blank=True)
    website_seo_score = models.IntegerField(null=True, blank=True)
    website_mobile_score = models.IntegerField(null=True, blank=True)
    website_issues = models.JSONField(default=list, blank=True)
    audit_run_at = models.DateTimeField(null=True, blank=True)

    # ── Enrichment (post-scrape signals — see outreach/enricher.py) ──
    # Populated by enrich_lead() Celery task, fired after import_leads
    # saves the lead row. Two phases:
    #   1. Homepage scrape — emails, social URLs, SSL, copyright year,
    #      PageSpeed.
    #   2. Google Custom Search fallback (only when website is blank) —
    #      tries to find FB / IG / a real website by name + city + state.

    # Social presence. Three biggest channels for SMBs; others go in
    # other_social_urls so we don't keep adding columns.
    facebook_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    other_social_urls = models.JSONField(default=list, blank=True)

    # Site-quality signals — cheap to derive from the homepage HTML,
    # all feed the scorer.
    has_ssl = models.BooleanField(
        null=True, blank=True,
        help_text=(
            "True when site is reachable on https://, False when only "
            "http:// works, NULL when not yet checked."))
    copyright_year = models.IntegerField(
        null=True, blank=True,
        help_text=("Year parsed from the footer's © string. Stale "
                   "(3+ years old) is a scoring signal."))
    has_generic_email = models.BooleanField(
        null=True, blank=True,
        help_text=(
            "True when the email we found lives on a free provider "
            "(gmail.com, yahoo.com, hotmail.com, aol.com, outlook.com) "
            "rather than the firm's own domain."))

    # What is actually at this domain. Blank means a real, reachable
    # site we may legitimately comment on.
    #
    # This exists because a parked domain is not a bad website, it is the
    # ABSENCE of one, and every site-quality signal is meaningless
    # against it. PageSpeed scored a Wix "domain isn't connected to a
    # site" placeholder 89/100 -- an excellent score for a page that does
    # not exist. Without this, the icebreaker generator would compliment
    # a parking page or criticise intake forms that are not there.
    #
    # NOT stored in website_issues: that field already holds PageSpeed
    # audit dicts and is not a flag set.
    site_status = models.CharField(
        max_length=20, blank=True, db_index=True,
        help_text=(
            "'' = live site. Otherwise site_parked / site_unreachable / "
            "site_bot_blocked. Set by outreach.enricher.classify_site."),
    )

    # Why the TLS handshake failed, when it did. A quotable finding:
    # "certificate expired" is a real problem worth an email, whereas a
    # 403 aimed at scrapers is not and must never be reported as one.
    tls_error = models.CharField(max_length=200, blank=True)

    # Enrichment lifecycle — task picks up rows where _completed_at
    # is NULL, sets _attempted_at on entry, _completed_at on success.
    # log is plain text appended by each enrichment step for forensics.
    enrichment_attempted_at = models.DateTimeField(null=True, blank=True)
    enrichment_completed_at = models.DateTimeField(null=True, blank=True)
    enrichment_log = models.TextField(blank=True)

    # Lead scoring
    score = models.IntegerField(default=0)
    temperature = models.CharField(
        max_length=10, choices=TEMPERATURE_CHOICES, default='cold'
    )

    # CRM status + source
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='new'
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='manual'
    )

    # Outreach tracking
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    next_followup_at = models.DateTimeField(null=True, blank=True)
    sequence_step = models.IntegerField(default=0)
    sequence_paused = models.BooleanField(default=False)
    unsubscribed = models.BooleanField(default=False)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    # Tags — comma-separated. Also stores "heard about us" answer from
    # contact form (see CLAUDE.md → Data Model Decisions).
    tags = models.CharField(max_length=500, blank=True)

    # What the visitor says they need — asked directly on the contact
    # form rather than inferred from the free-text message. Drives lead
    # triage (a build enquiry and an SEO retainer enquiry are answered
    # very differently) and is the `service_interest` param the §10
    # event spec asks for on contact_form_submit.
    #
    # Free text, not choices, because the form's option list will move
    # as services change and old rows must keep meaning what they said.
    service_interest = models.CharField(max_length=100, blank=True)

    # Visitor-supplied free text (contact form message, audit-tool email
    # capture notes, etc). Distinct from `notes` which is internal-only.
    inquiry_text = models.TextField(blank=True)

    # ── Email verification (outreach/verify.py) ────────────────────────
    # The stage whose absence caused 416 sends to return zero replies:
    # 111 went to info@, 97 to consumer gmail, and several to addresses
    # scraped off the wrong page entirely. A lead does not enter a
    # campaign until this says it is sendable.
    email_verification_status = models.CharField(
        max_length=20, default='pending', db_index=True,
        help_text=(
            'Set by outreach.verify. Role addresses and hard-invalid '
            'mailboxes never reach a campaign.'),
    )
    email_verified_at = models.DateTimeField(null=True, blank=True)

    # ── Instantly (outreach/instantly.py) ──────────────────────────────
    # Instantly owns sending; Django owns everything up to the push.
    instantly_lead_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text='Instantly\'s own id, returned when the lead is pushed.')
    pushed_to_instantly_at = models.DateTimeField(null=True, blank=True)
    campaign = models.ForeignKey(
        'OutreachCampaign', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='leads',
    )

    # The single personalised sentence Claude writes per lead, pushed to
    # Instantly as a custom variable and referenced by the campaign
    # template. This is the "one specific true thing" that separates a
    # cold email worth reading from a mail merge.
    icebreaker = models.TextField(
        blank=True,
        help_text=(
            'One personalised opening line, generated from enrichment '
            'signals. Pushed to Instantly as {{icebreaker}}.'),
    )
    icebreaker_generated_at = models.DateTimeField(null=True, blank=True)

    # ── Warm-opener material ───────────────────────────────────────────
    # The facts an icebreaker may say something friendly about. Stored as
    # real fields rather than left inside `notes` because the fabrication
    # guard has to CHECK them: "practising since 1998" is exactly the
    # kind of flattering detail a model invents, and a claim we cannot
    # verify is a claim we cannot ship.
    founded_year = models.IntegerField(
        null=True, blank=True,
        help_text='Year the firm was founded, when the source supplies it.')
    practice_areas = models.CharField(
        max_length=500, blank=True,
        help_text='Comma-separated, from the source. e.g. "estate planning, probate".')

    # ── Manual review (outreach/review.py) ─────────────────────────────
    # Set when the company NAME contradicts the industry the source
    # claims. Apollo tags "Bwa Video, Inc." and "Kinney Recruiting" as
    # Legal Services, and no actor-side filter can exclude a recruiting
    # company the source calls a law practice.
    #
    # A flag, not a block: the signal is a heuristic on a name, and
    # auto-discarding on a guess loses real prospects silently. This
    # fails in the recoverable direction.
    needs_review = models.BooleanField(default=False, db_index=True)
    review_reason = models.CharField(max_length=255, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    # Internal CRM scratch — not visible to the lead.
    notes = models.TextField(blank=True)

    # IP captured for contact-form / audit-tool / scraped leads.
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    # Referral attribution — set from `request.session['referral_code']`
    # when a Lead is created from the contact form (Phase 7 Part 2).
    referral_code = models.CharField(max_length=20, blank=True)

    # Add-ons the lead opted into when booking a call (ServiceTier slugs,
    # e.g. ['maintenance-growth']). Carries the "10% off first month"
    # promise — auto-applied as a Stripe coupon when they check out for an
    # add-on of the same category. Set by scheduler.confirm_slot.
    opted_in_addons = models.JSONField(default=list, blank=True)
    opted_in_addons_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-score', '-created_at']
        # No unique_together — uniqueness is enforced in code via
        # outreach.deduplication.is_duplicate (fuzzy match on
        # firm_name + city + state). See CLAUDE.md.

    def __str__(self):
        loc = f', {self.city}, {self.state}' if self.city else ''
        return f'{self.firm_name}{loc}'


class LeadNote(models.Model):
    """Internal CRM note attached to a Lead, with its own timestamp."""

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='lead_notes'
    )
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Note for {self.lead.firm_name} at {self.created_at:%Y-%m-%d}'


class EmailTemplateVariant(models.Model):
    """
    An approved angle for cold outreach copy — what the agent picks from
    and tests between. Replaces the old pattern of prompt strings baked
    into a dict in ``sender.py`` (the original audit's finding #6: there
    was no Sequence or EmailTemplate model at all, so nothing could
    compare one approach against another).

    WHAT LIVES HERE vs WHAT LIVES IN CODE
    -------------------------------------
    ``angle_instructions`` holds ONLY the per-variant angle — the thing
    being tested (security-first vs speed vs local-competitor). Aspired's
    voice, tone and hard constraints stay in ``sender._system_prompt()``
    in code, unchanged by variant, and the angle is appended to it at
    draft time. Rationale: how Aspired *sounds* is a constant the
    business sets once; letting it drift per variant would mean A/B
    testing brand voice by accident.

    GUARDRAIL (§1.2): the agent may choose among ``active=True`` rows and
    test combinations of them. It may NOT freehand a new angle into live
    rotation — ``propose_new_template_variant`` writes ``active=False``
    plus an AIEmployeeAction awaiting approval, and a human flips it on.
    """

    PROPOSED_BY_CHOICES = [
        ('human', 'Human'),
        ('agent', 'Agent'),
    ]

    name = models.CharField(
        max_length=100, help_text='e.g. "Security-first", "Slow site"')
    sequence_step = models.IntegerField(
        db_index=True, help_text='Which of the 4 touches this is for (1-4).')
    angle_instructions = models.TextField(
        help_text=(
            'The per-variant angle, appended to the shared system prompt '
            'at draft time. Editable without a deploy.'))

    # active=False means "proposed, awaiting approval" — the default, so
    # an agent-proposed variant can never go live by accident.
    active = models.BooleanField(default=False, db_index=True)
    proposed_by = models.CharField(
        max_length=20, default='human', choices=PROPOSED_BY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    # Rolling stats, denormalised on purpose: these are read on every
    # agent run to pick a variant, and that must not become a join across
    # the whole EmailSent table each time. Updated as the linked
    # EmailSent rows change state.
    sends = models.IntegerField(default=0)
    opens = models.IntegerField(default=0)
    replies = models.IntegerField(default=0)
    bookings = models.IntegerField(default=0)

    class Meta:
        ordering = ['sequence_step', 'name']
        verbose_name = 'Email Template Variant'
        verbose_name_plural = 'Email Template Variants'

    def __str__(self):
        state = 'active' if self.active else 'inactive'
        return f'Step {self.sequence_step} — {self.name} ({state})'

    @property
    def reply_rate(self):
        """Replies per send, 0.0 when nothing has been sent yet."""
        return (self.replies / self.sends) if self.sends else 0.0


class EmailSent(models.Model):
    """
    Outreach email — generated by ``outreach.sender.generate_cold_email``
    (cold) or ``outreach.reply_sender.draft_reply`` (reply). Lifecycle:

        pending_approval ─→ approved ─→ sent
                       └─→ rejected

    ``pending_approval`` rows wait for a human in the Approvals queue;
    ``approved`` rows are picked up by ``send_approved_emails_task`` and
    actually dispatched via SendGrid (``sent_at`` set then, not at
    creation). The trust-level dial in OutreachSettings decides whether
    new rows are auto-promoted past ``pending_approval`` at generation
    time — see ``outreach.gating.should_queue_for_approval``.
    """

    STATUS_CHOICES = [
        ('pending_approval', 'Pending approval'),
        ('approved', 'Approved — waiting to send'),
        ('sent', 'Sent'),
        ('rejected', 'Rejected'),
    ]
    KIND_CHOICES = [
        ('cold', 'Cold outreach'),
        ('reply', 'Reply'),
    ]

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='emails_sent'
    )
    # Reply emails point back at the inbound EmailReply they answer; cold
    # emails leave this null.
    in_reply_to = models.ForeignKey(
        'EmailReply', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='outbound_replies',
    )

    # Which approved angle produced this copy. Null for reply drafts and
    # for every row written before variants existed. This FK is what
    # powers the EmailTemplateVariant stats rollup and the rotation maths
    # — without it there is no way to attribute a reply to an angle.
    template_variant = models.ForeignKey(
        'EmailTemplateVariant', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='emails_sent',
    )

    kind = models.CharField(
        max_length=10, choices=KIND_CHOICES, default='cold'
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending_approval',
        db_index=True,
    )

    subject = models.CharField(max_length=255)
    body = models.TextField()
    from_email = models.EmailField()

    sequence_step = models.IntegerField(default=1)

    # Engagement (set after status='sent' by inbound trackers).
    opened = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked = models.BooleanField(default=False)
    clicked_at = models.DateTimeField(null=True, blank=True)
    replied = models.BooleanField(default=False)
    replied_at = models.DateTimeField(null=True, blank=True)

    # Approval/dispatch metadata. created_at is the generation moment;
    # sent_at is when SendGrid accepted it. They will differ by minutes
    # (auto-send) or hours/days (queued for approval).
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.CharField(max_length=255, blank=True)

    # Message-ID we wrote into the outgoing email's headers; reply
    # ingestion uses this to thread inbound replies back to the right
    # EmailSent. Populated when the drainer actually dispatches.
    message_id_header = models.CharField(
        max_length=255, blank=True, db_index=True,
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'kind', '-created_at']),
        ]

    def __str__(self):
        return (
            f'Email to {self.lead.firm_name} — '
            f'Step {self.sequence_step} ({self.status})'
        )


class EmailReply(models.Model):
    """Inbound reply from a Lead — classified for routing."""

    CLASSIFICATION_CHOICES = [
        ('interested', 'Interested'),
        ('not_interested', 'Not Interested'),
        ('wrong_person', 'Wrong Person'),
        ('maybe_later', 'Maybe Later'),
        ('already_have_someone', 'Already Have Someone'),
        ('question', 'Question — Needs You'),
        ('unclear', 'Unclear — Needs You'),
        ('hostile', 'Hostile — Needs You'),
        ('unsubscribe', 'Unsubscribe Request'),
    ]

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='replies'
    )
    email_sent = models.ForeignKey(
        EmailSent, on_delete=models.SET_NULL, null=True, blank=True
    )

    classification = models.CharField(
        max_length=30, choices=CLASSIFICATION_CHOICES, blank=True
    )

    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    received_at = models.DateTimeField(auto_now_add=True)
    needs_human = models.BooleanField(default=False)
    handled = models.BooleanField(default=False)
    handled_at = models.DateTimeField(null=True, blank=True)

    ai_suggested_reply = models.TextField(blank=True)

    # RFC 5322 Message-ID of the inbound mail itself — used by
    # ``outreach.reply_ingest`` to guarantee idempotency across IMAP
    # poll runs. ``null=True`` because some old EmailReply rows
    # pre-date this column; ``unique=True`` is safe because NULL
    # values don't collide in unique indexes on either SQLite or
    # Postgres.
    inbound_message_id = models.CharField(
        max_length=255, null=True, blank=True,
        unique=True, db_index=True,
    )

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        label = self.classification or 'unclassified'
        return f'Reply from {self.lead.firm_name} — {label}'


class SuppressionList(models.Model):
    """Permanent do-not-contact list. Unsubscribes are forever."""

    email = models.EmailField(unique=True)
    domain = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=100, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-added_at']
        verbose_name = 'Suppression List Entry'
        verbose_name_plural = 'Suppression List'

    def __str__(self):
        return self.email


class OutreachSettings(models.Model):
    """Singleton — one row, ever. Controls outreach automation behavior."""

    TRUST_LEVEL_CHOICES = [
        (1, 'Level 1 — Approve every email'),
        (2, 'Level 2 — Auto-send cold, approve replies'),
        (3, 'Level 3 — Auto-send cold + simple replies'),
        (4, 'Level 4 — Full auto except flagged'),
        (5, 'Level 5 — Fully autonomous'),
    ]

    trust_level = models.IntegerField(
        choices=TRUST_LEVEL_CHOICES, default=1
    )
    daily_send_cap = models.IntegerField(default=15)
    warming_start_date = models.DateField(null=True, blank=True)
    outreach_active = models.BooleanField(default=False)

    # ── §1.3 — spend guardrails. TWO SEPARATE CAPS, deliberately. ──
    #
    # Claude and Apify bill on completely different shapes: Claude is
    # per-token and accumulates smoothly, Apify is per-run/compute-unit
    # and one bad call can burn a chunk of budget in a single request.
    # Sharing one pool means a runaway scrape silently eats the reasoning
    # budget and Prospect goes quiet for the rest of the day with no
    # obvious cause. Keep them independent so exhausting one never
    # disables the other.

    # Cap A — Claude / LLM tokens only. Enforced in code, not by prompt:
    # once the day's spend crosses this, the LLM tools refuse and the
    # agent is told to wrap up. Ledger = today's AIEmployeeRun.spend_usd.
    daily_ai_spend_cap_usd = models.DecimalField(
        max_digits=8, decimal_places=2, default=10.00,
        help_text=(
            'Hard daily USD ceiling for Claude/LLM spend ONLY. Apify has '
            'its own separate quota below. Set to 0 to stop the agent '
            'reasoning entirely.'),
    )

    # Cap B — Apify lead sourcing. Bounded by RUN COUNT and RESULT COUNT
    # rather than dollars: the agent knows how many runs it has left far
    # more reliably than it can predict an actor's compute cost, and a
    # run ceiling is the thing that actually stops a runaway scrape.
    # Consumed by outreach/apify_source.py when §3 is built.
    # Defaults sized against the ACTUAL plan, not guessed. The Apify
    # account is on the FREE $5/month tier, and the actor bills $0.02 per
    # run start plus $0.002 per lead. So:
    #     1 run/day x 50 leads = $0.12/day = ~$3.60/month
    # The original 3 x 100 would have been $0.66/day — the entire monthly
    # allowance gone in about 8 days.
    apify_max_runs_per_day = models.IntegerField(
        default=1,
        help_text=(
            'Maximum Apify actor runs Prospect may start per day. '
            'Separate from the Claude spend cap on purpose — a runaway '
            'scrape must not eat the reasoning budget. 0 disables '
            'sourcing.'),
    )
    apify_max_results_per_run = models.IntegerField(
        default=50,
        help_text=(
            'Maximum leads requested per Apify actor run. At $0.002/lead '
            'this is the main cost lever: 50 leads is ~$0.12 a run. The '
            'actor own default is 100000 — never let a run through '
            'without this clamp.'),
    )

    # Google Places lookups — the join that gives an Apify lead something
    # true to say. Text Search (New) bills about $0.032 per call on the
    # Essentials SKU, against Google's $200/month free credit, so 150/day
    # is roughly $4.80/day and comfortably inside the credit.
    #
    # Capped separately from Apify and Claude for the same reason those
    # are separate from each other: three budgets that cannot cannibalise
    # one another, so a runaway in any one leaves the others working.
    places_max_lookups_per_day = models.IntegerField(
        default=150,
        help_text=(
            'Maximum Google Places profile lookups per day. Only ever '
            'spent on QUALIFIED leads — verified, contactable, and not '
            'held for review. 0 disables the lookup.'),
    )

    # ── The send switch ────────────────────────────────────────────────
    #
    # Pushing a lead into an Instantly campaign is the last reversible
    # step before a stranger receives mail. This is the switch that
    # governs it, and it defaults OFF.
    #
    # It is deliberately NOT the only gate. ``instantly.warmup_readiness``
    # independently measures whether the mailboxes are actually warm, and
    # ``push_leads`` requires both. A switch alone would be one mis-click
    # away from sending 270 emails/day from mailboxes that finished setup
    # yesterday, which is precisely how a domain gets burned before it has
    # sent anything worth reading.
    instantly_sending_enabled = models.BooleanField(
        default=False,
        help_text=(
            'OFF until the sending mailboxes are warmed. Turning it on '
            'does NOT bypass the warmup check - both must pass before any '
            'lead is pushed to a campaign.'),
    )

    # Minimum Instantly warmup score before a mailbox counts as usable.
    # Instantly reports 0-100; a mailbox mid-ramp sits well below this.
    min_warmup_score = models.IntegerField(
        default=90,
        help_text='Mailboxes below this score do not count toward the '
                  'readiness check.')

    # Warmup needs calendar time as well as a good score - a brand new
    # mailbox can show a flattering score days before providers trust it.
    min_warmup_days = models.IntegerField(
        default=14,
        help_text='Days a mailbox must have been warming, regardless of '
                  'its score.')

    min_ready_mailboxes = models.IntegerField(
        default=3,
        help_text='How many mailboxes must pass before sending is '
                  'allowed. Rotation needs more than one.')

    # Counter — resets at midnight via Celery beat task (added in later week).
    emails_sent_today = models.IntegerField(default=0)
    last_reset_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'Outreach Settings'
        verbose_name_plural = 'Outreach Settings'

    def __str__(self):
        return f'Outreach Settings — Level {self.trust_level}'

    @classmethod
    def load(cls):
        """Singleton accessor — gets or creates the one row at pk=1."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ScrapeJob(models.Model):
    """
    A standing scrape recipe — niche + city/state + source. The daily
    Celery beat task ``run_scrape_jobs_task`` runs every row where
    ``active=True`` once per 24h, feeding the discovered leads into
    ``outreach.pipeline.import_leads`` (which dedupes against existing
    Lead rows automatically).

    Set ``active=False`` to pause without deleting; the dashboard's
    history of last_run / leads_imported stays for tuning.

    The shape mirrors the manual scrape form so the same view function
    handles both — the only difference is one runs synchronously when
    the operator clicks, the other runs via Celery.
    """

    SOURCE_CHOICES = [
        ('apify', 'Apify — contacts with emails'),
        ('google_maps', 'Google Maps'),
        ('texas_bar', 'Texas State Bar'),
        ('georgia_bar', 'Georgia State Bar'),
    ]

    name = models.CharField(
        max_length=120,
        help_text='Friendly label shown on the scrape dashboard.',
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    niche = models.CharField(
        max_length=120,
        help_text=(
            'Google Maps: free-text search niche (e.g. "personal injury '
            'lawyer", "dentist"). Bar scrapers: the practice area string '
            'exactly as it appears in the directory.'),
    )
    city = models.CharField(max_length=100)
    state = models.CharField(
        max_length=2,
        help_text='TX or GA only (bar scrapers cover those two states).',
    )
    max_results = models.IntegerField(default=20)

    active = models.BooleanField(default=True, db_index=True)

    # Run bookkeeping — only the latest pass is kept.
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_imported = models.IntegerField(default=0)
    last_run_skipped = models.IntegerField(default=0)
    last_run_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-active', 'name']

    def __str__(self):
        return f'{self.name} ({self.source})'


class ApifyRun(models.Model):
    """
    One Apify actor run — the ledger the Apify quota reads
    (COLD_OUTREACH_AGENT.md §1.3 cap B, §3).

    Deliberately separate from ``AIEmployeeRun.spend_usd``, which is the
    Claude ledger. Apify bills per run and per lead, and a single bad call
    can burn a large slice of budget at once; letting the two share a pool
    would mean one runaway scrape silently starves the reasoning budget.

    ``estimated_cost_usd`` is written BEFORE the run starts. A run that
    dies mid-flight still consumed compute, so costing it only on success
    under-reports precisely when it matters most. ``actual_cost_usd`` is
    filled in afterwards from Apify's own accounting when available.
    """

    STATUS_CHOICES = [
        ('running', 'Running'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('refused', 'Refused — quota or budget'),
    ]

    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, default='running', choices=STATUS_CHOICES)

    actor_id = models.CharField(max_length=64)
    apify_run_id = models.CharField(max_length=64, blank=True)
    dataset_id = models.CharField(max_length=64, blank=True)

    # What we asked for, in the actor's own terms.
    label = models.CharField(max_length=200, blank=True)
    search_input = models.JSONField(default=dict, blank=True)

    results_requested = models.IntegerField(default=0)
    results_returned = models.IntegerField(default=0)
    leads_imported = models.IntegerField(default=0)

    estimated_cost_usd = models.DecimalField(
        max_digits=8, decimal_places=4, default=0)
    actual_cost_usd = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True)

    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name = 'Apify Run'
        verbose_name_plural = 'Apify Runs'

    def __str__(self):
        return (f'{self.label or self.actor_id} — {self.status} '
                f'({self.results_returned} leads, '
                f'${self.actual_cost_usd or self.estimated_cost_usd})')

    @property
    def cost_usd(self):
        """Actual cost where Apify reported one, else our estimate."""
        return (self.actual_cost_usd
                if self.actual_cost_usd is not None
                else self.estimated_cost_usd)


class BraveSearchUsage(models.Model):
    """
    Per-month Brave Search API query counter. One row per month;
    incremented atomically by ``outreach.enricher._brave_search``
    after each successful API call.

    Drives the usage banner on /admin-dashboard/leads/ so the admin
    can see where they are against Brave's free 2000/mo tier before
    queries start costing $3/1000.

    Why a model + not a cache key: needs to survive Redis restarts,
    needs a 12-month history for trend visibility later, and writes
    are cheap (one INSERT-OR-UPDATE per Brave call ≤ 3/lead).
    """

    # First day of the month — '2026-05-01' covers all of May 2026.
    # Unique so update_or_create(year_month=...) is a single hit.
    year_month = models.DateField(unique=True)
    query_count = models.IntegerField(default=0)
    last_query_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-year_month']
        verbose_name = 'Brave Search Usage'
        verbose_name_plural = 'Brave Search Usage'

    def __str__(self):
        return (f'{self.year_month:%B %Y} — {self.query_count} '
                f'queries')

    @classmethod
    def _current_month(cls):
        today = timezone.now().date()
        return today.replace(day=1)

    @classmethod
    def increment(cls, n=1):
        """Atomic increment for THIS month's row. Creates the row
        on first call of a new month. Safe to call from concurrent
        Celery workers — uses F() expression."""
        from django.db.models import F
        ym = cls._current_month()
        cls.objects.get_or_create(year_month=ym)
        cls.objects.filter(year_month=ym).update(
            query_count=F('query_count') + n)

    @classmethod
    def current(cls):
        """This month's count (0 when no queries yet this month)."""
        ym = cls._current_month()
        row = cls.objects.filter(year_month=ym).first()
        return row.query_count if row else 0


class Offer(models.Model):
    """What the prospect gets, and how they start. The A/B variable.

    WHY THIS IS A ROW AND NOT A CONSTANT
    ------------------------------------
    Offers began life as a dict in ``outreach/sequences.py``. That made
    changing one a code edit and a deploy, which is wrong for the same
    reason hardcoded prices are wrong (CLAUDE.md: "NEVER hardcode prices
    ... always query ServiceTier"). It also made the whole point
    unreachable: an agent that measures which offer wins but cannot act
    on it has learned nothing useful.

    The guardrail from EmailTemplateVariant carries over intact --
    ``active`` defaults False, so an agent-proposed offer is a proposal
    awaiting a human, never something that starts going out on its own.

    THE THREE PROPERTIES OF AN OFFER THAT WORKS
    -------------------------------------------
      1. Minimises financial risk   -> free, or money back
      2. Minimises friction         -> one word starts it
      3. Cheap for us to produce    -> or it stops scaling the moment it
                                       starts working

    ``fulfilment_cost`` records the third honestly, because it is the one
    that gets ignored and then hurts. An offer with a 10% reply rate that
    costs four hours to honour is a trap: succeed and you have sold
    yourself into unpaid full-time work.
    """

    PROPOSED_BY_CHOICES = [
        ('human', 'Human'),
        ('agent', 'Agent'),
    ]

    key = models.SlugField(
        max_length=60, unique=True,
        help_text='Stable identifier, e.g. "security_review".')
    name = models.CharField(
        max_length=120, help_text='Shown in the admin and campaign names.')
    appeals_to = models.CharField(
        max_length=120, blank=True,
        help_text='What motivation this offer targets. Keeps the set '
                  'genuinely different rather than six rewordings.')
    fulfilment_cost = models.TextField(
        blank=True,
        help_text='Honest note on what honouring this costs YOU. Read it '
                  'before scaling an offer that is working.')

    # The three slots the sequence template substitutes.
    pitch = models.TextField(
        help_text='Touch 1: the full offer, in your voice.')
    restate = models.TextField(
        help_text='Touch 2: the offer in one clause, no leading capital. '
                  'Reads as "...what I am offering: <restate>."')
    ask = models.TextField(
        help_text='The one-line, low-friction call to action.')

    active = models.BooleanField(
        default=False, db_index=True,
        help_text='False = proposed, awaiting approval. Default False so '
                  'nothing an agent writes can start sending itself.')
    proposed_by = models.CharField(
        max_length=20, default='human', choices=PROPOSED_BY_CHOICES)

    # Denormalised on purpose — read on every campaign summary, and a
    # join across EmailSent for each would be pointless work.
    sends = models.IntegerField(default=0)
    replies = models.IntegerField(default=0)
    positive_replies = models.IntegerField(default=0)
    bookings = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-active', 'name']

    def __str__(self):
        return f'{self.name} ({"active" if self.active else "inactive"})'

    @property
    def reply_rate(self):
        """Replies per send, 0.0 before anything has gone out."""
        return (self.replies / self.sends) if self.sends else 0.0

    def as_dict(self):
        """The shape ``sequences.build_steps`` substitutes from."""
        return {
            'name': self.name,
            'appeals_to': self.appeals_to,
            'fulfilment_cost': self.fulfilment_cost,
            'pitch': self.pitch,
            'restate': self.restate,
            'ask': self.ask,
        }


class OutreachCampaign(models.Model):
    """
    One niche × geography segment, mapped to one Instantly campaign.

    WHY SEGMENTS RATHER THAN ONE BIG CAMPAIGN
    -----------------------------------------
    Copy that references something specific and true cannot be written
    for a blended list. "I work with personal injury firms in Houston"
    is a sentence; "I work with businesses" is noise. Segmenting also
    makes reply rate a per-niche measurement, which is the only way to
    learn which niche actually wants this rather than guessing.

    Four to start: TX law, GA law, TX dental, GA dental.

    ``instantly_campaign_id`` is the join to the sending side. It stays
    blank until the campaign is created in Instantly (either through
    their UI or via ``outreach.instantly.create_campaign``), and nothing
    can be pushed until it is set.
    """

    name = models.CharField(
        max_length=120,
        help_text='e.g. "TX — Personal Injury", "GA — Dental".')
    slug = models.SlugField(max_length=140, unique=True)

    # Targeting — also the search input when sourcing for this campaign.
    niche = models.CharField(
        max_length=120,
        help_text='Search term / industry, e.g. "personal injury lawyer".')
    business_type = models.CharField(
        max_length=100, blank=True,
        help_text='Stamped onto imported leads. Blank = infer from source.')
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=2, blank=True)

    # Which offer this campaign makes. The A/B arm: one campaign per
    # offer means per-campaign analytics IS per-offer reply rate, with
    # exactly one variable differing between arms.
    offer = models.ForeignKey(
        Offer, on_delete=models.PROTECT,
        null=True, blank=True, related_name='campaigns',
        help_text='Null falls back to the default offer at build time.')

    instantly_campaign_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text=(
            'Instantly campaign UUID. Blank means leads cannot be pushed '
            'yet — create the campaign in Instantly first.'),
    )

    # active=False pauses pushing without deleting the segment or losing
    # its history. Default False so a newly-created campaign cannot start
    # receiving leads before someone has looked at it.
    active = models.BooleanField(default=False, db_index=True)

    # How many leads this arm should collect before it stops taking more.
    #
    # This exists to make a statistically readable A/B possible. Six arms
    # sharing one city's ~750 sendable leads is 125 each; at a 3% reply
    # rate that is under four replies per arm, which cannot distinguish a
    # 2% offer from a 5% one. Running two or three arms to 300-400 each
    # can. The cap is what stops an arm quietly eating the whole pool
    # before the comparison arm has filled.
    #
    # 0 means unlimited — correct once an offer has WON and is simply the
    # one being sent, rather than one side of a test.
    lead_target = models.PositiveIntegerField(
        default=0,
        help_text=(
            'Stop assigning leads to this arm once it holds this many. '
            '0 = unlimited. Use 300-400 per arm for a readable A/B.'),
    )

    # Bookkeeping.
    leads_pushed = models.IntegerField(default=0)
    last_push_at = models.DateTimeField(null=True, blank=True)
    last_push_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-active', 'name']
        verbose_name = 'Outreach Campaign'
        verbose_name_plural = 'Outreach Campaigns'

    def __str__(self):
        state = 'active' if self.active else 'paused'
        return f'{self.name} ({state})'

    @property
    def is_pushable(self):
        """Whether push_leads may target this campaign at all."""
        return bool(self.active and self.instantly_campaign_id)

    @property
    def is_full(self):
        """True when this arm has collected its target sample."""
        if not self.lead_target:
            return False
        return self.leads.count() >= self.lead_target

    @property
    def accepts_leads(self):
        """Whether assignment may put another lead in this arm.

        Deliberately stricter than ``is_pushable``: an arm that has hit
        its sample target is still pushable (its existing leads must
        finish sending) but must stop accepting new ones, or the target
        means nothing.
        """
        return self.is_pushable and not self.is_full


class InstantlyEvent(models.Model):
    """
    Raw webhook event from Instantly, stored before it is interpreted.

    WHY THE RAW ROW IS KEPT
    -----------------------
    The reply-ingest path had no record of what it received, so when it
    filed ten Google Ads notifications as prospect replies there was
    nothing to audit — the mistake was only visible by reading the
    resulting EmailReply rows and noticing they made no sense. Keeping
    the payload means a misclassification can be diagnosed and replayed
    rather than guessed at.

    ``dedupe_key`` exists because webhooks are at-least-once delivery.
    Instantly will resend on any non-2xx, and marking a lead unsubscribed
    twice is harmless while creating a duplicate EmailReply is not.
    """

    EVENT_CHOICES = [
        ('reply_received', 'Reply received'),
        ('email_sent', 'Email sent'),
        ('email_opened', 'Email opened'),
        ('link_clicked', 'Link clicked'),
        ('email_bounced', 'Email bounced'),
        ('lead_unsubscribed', 'Lead unsubscribed'),
        ('lead_interested', 'Lead marked interested'),
        ('lead_not_interested', 'Lead marked not interested'),
        ('campaign_completed', 'Campaign completed for lead'),
        ('unknown', 'Unknown / unhandled'),
    ]

    event_type = models.CharField(
        max_length=40, choices=EVENT_CHOICES, default='unknown',
        db_index=True)
    # Instantly's own event name, verbatim, before mapping. Kept because
    # their vocabulary changes and an unmapped event should still be
    # diagnosable from the row rather than only from logs.
    raw_event_type = models.CharField(max_length=80, blank=True)

    lead = models.ForeignKey(
        Lead, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='instantly_events')
    lead_email = models.EmailField(blank=True, db_index=True)
    campaign = models.ForeignKey(
        OutreachCampaign, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='events')

    payload = models.JSONField(default=dict, blank=True)

    # Unique per logical event. Built from the event type plus whatever
    # stable identifiers the payload carries; see instantly_webhook.
    dedupe_key = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True)

    processed = models.BooleanField(default=False, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-received_at']
        verbose_name = 'Instantly Event'
        verbose_name_plural = 'Instantly Events'

    def __str__(self):
        who = self.lead_email or (self.lead.firm_name if self.lead else '?')
        return f'{self.event_type} — {who}'


class LeadReviewQueue(Lead):
    """Proxy over Lead: the manual-review queue as its own admin page.

    A proxy rather than a new table -- these ARE leads, they just need a
    glance before they can be emailed, and giving the queue its own
    changelist means the reviewer never has to filter the main lead list
    to find them.
    """

    class Meta:
        proxy = True
        verbose_name = 'Lead awaiting review'
        verbose_name_plural = 'Leads awaiting review'
