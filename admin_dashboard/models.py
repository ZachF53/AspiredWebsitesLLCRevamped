"""Admin dashboard models."""

from django.db import models

from core.models import TimestampedModel


class DeploymentLog(TimestampedModel):
    """A record of a deployment run — surfaced in the deploy dashboard."""

    DEPLOY_TYPE_CHOICES = [
        ('fresh', 'Fresh Server Deploy'),
        ('redeploy', 'Code Update (Re-deploy)'),
        ('client', 'Client Site Deploy'),
    ]

    deploy_type = models.CharField(max_length=20, choices=DEPLOY_TYPE_CHOICES)
    server_ip = models.CharField(max_length=50, blank=True)
    domain = models.CharField(max_length=200, blank=True)
    client = models.ForeignKey(
        'clients.ClientProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='deployment_logs',
    )
    # Phase A — deployments target a specific Website (one Droplet per
    # build). Account FK kept for legacy-row resolution.
    account_new = models.ForeignKey(
        'clients.Account',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='deployment_logs_new',
    )
    website_new = models.ForeignKey(
        'clients.Website',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='deployment_logs_new',
    )
    github_repo = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    success = models.BooleanField(default=True)
    deployed_by = models.CharField(max_length=100, default='Zachery Long')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Deployment Log'
        verbose_name_plural = 'Deployment Logs'

    def __str__(self):
        return f'{self.deploy_type} — {self.domain} — {self.created_at.date()}'


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4.5 — AI assistant audit log
# ─────────────────────────────────────────────────────────────────────────────

class AIAssistantLog(TimestampedModel):
    """One row per executed AI-assistant command. Append-only audit
    trail so we can review what the assistant did + when, alongside
    the existing ProjectStageLog / changelog systems.

    Parse-only commands (when the operator types something but cancels
    before confirm) are NOT logged — only executed mutations land here.
    """

    operator = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ai_assistant_logs',
    )
    client = models.ForeignKey(
        'clients.ClientProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ai_assistant_logs',
    )
    # Canonical subject of the command. This model had no canonical FK at
    # all, so the planned legacy drop would have removed `client` and
    # turned an append-only audit trail into a list of actions with no
    # record of who they were performed against -- the one property an
    # audit trail exists to have. SET_NULL matches `client`: losing the
    # subject must never delete the evidence that something happened.
    website_new = models.ForeignKey(
        'clients.Website',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ai_assistant_logs_new',
    )
    raw_command = models.TextField(blank=True)
    intent = models.CharField(max_length=80, blank=True)
    args = models.JSONField(default=dict, blank=True)
    success = models.BooleanField(default=False)
    result_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI Assistant Log'
        verbose_name_plural = 'AI Assistant Logs'

    def __str__(self):
        return f'{self.intent} ({"ok" if self.success else "FAIL"}) — {self.created_at}'


# ──────────────────────────────────────────────────────────────────────
# AI Employees — the agent registry + run log (COLD_OUTREACH_AGENT.md §8.1)
#
# These live here, not in outreach/, on purpose: the registry is designed
# to hold agents that have nothing to do with outreach (a Research Agent,
# an SEO Audit Agent), and the cockpit views + nav badge are already in
# this app. Putting them in outreach/ would force a future SEO agent to
# import from the outreach app.
#
# Plain integer PKs rather than TimestampedModel/UUID (unlike the two
# models above). TimestampedModel exists so Aspired and Moonieful IDs
# never collide across the sync bridge — none of this data crosses that
# bridge, and these are high-volume log rows.
# ──────────────────────────────────────────────────────────────────────


class AIEmployee(models.Model):
    """Registry row — one per distinct agent."""

    REASONING_EFFORT_CHOICES = [
        ('low', 'Low — cheapest, shallow reasoning'),
        ('medium', 'Medium — default'),
        ('high', 'High — the API default'),
        ('xhigh', 'Extra high — for hard agentic work'),
        ('max', 'Max — correctness over cost'),
    ]

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    role_description = models.TextField(blank=True)

    # Paused = no scheduled runs. Manual "Wake now" still works, matching
    # OutreachSettings.outreach_active as a fast kill-switch.
    active = models.BooleanField(default=True)
    run_interval_minutes = models.IntegerField(default=60)

    # Deliberately defaulted BELOW the API's own 'high'. Same posture as
    # OutreachSettings.trust_level starting at 1: start conservative and
    # raise it once the daily digest shows the extra spend earns its keep.
    reasoning_effort = models.CharField(
        max_length=10, choices=REASONING_EFFORT_CHOICES, default='medium',
        help_text=(
            'Claude effort level for this agent\'s reasoning. Higher costs '
            'more per run. Raise once the daily digest justifies it.'),
    )

    # §5.3 — the summary this employee wrote at the end of its last run,
    # fed back into the next run's system prompt so it adapts across runs
    # instead of re-deriving everything from zero.
    last_journal_entry = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'AI Employee'
        verbose_name_plural = 'AI Employees'

    def __str__(self):
        return f'{self.name} ({"active" if self.active else "paused"})'


class AIEmployeeRun(models.Model):
    """One row per wake-up cycle — the running log."""

    TRIGGER_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('manual', 'Manual'),
        ('reply_webhook', 'Reply'),
    ]
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    employee = models.ForeignKey(
        AIEmployee, on_delete=models.CASCADE, related_name='runs')
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, default='running', choices=STATUS_CHOICES)
    # Human-readable journal entry. This is what the run log renders and
    # what feeds the next run's system prompt (§5.3). Unchanged.
    summary = models.TextField(blank=True)

    # The actual conversation, in Anthropic wire shape: a list of
    # {role, content} dicts where content carries real text / tool_use /
    # tool_result blocks — NOT a flattened summary.
    #
    # Stored now specifically so a conversational chat pane stays cheap
    # later. Adding one means passing prior turns back into
    # claude_agent_loop, which needs genuine message objects; rebuilding
    # those from `summary` after the fact is lossy and would cost a
    # migration plus a backfill. This is additive alongside `summary`,
    # not a replacement, and nothing reads it yet.
    #
    # Not built here: the chat UI, and per-employee thread persistence
    # across runs. This is one run's history only — the seed those would
    # grow from.
    message_history = models.JSONField(
        default=list, blank=True,
        help_text=(
            'Raw Anthropic message list for this run. Reserved for future '
            'conversational replay; nothing reads it yet.'),
    )

    steps_used = models.IntegerField(default=0)

    # §1.3 — the run-scoped spend ledger the daily cap reads. Written
    # incrementally as the loop runs (NOT only at the end) so a crashed
    # or still-running run still counts against today's budget.
    #
    # reporting.models.ClaudeUsage is a per-MONTH, per-model rollup and
    # cannot answer "how much today" — that is why this field exists
    # rather than reusing it. ClaudeUsage keeps doing its own job.
    spend_usd = models.DecimalField(
        max_digits=8, decimal_places=4, default=0)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.employee.name} run {self.started_at:%Y-%m-%d %H:%M} ({self.status})'


class AIEmployeeAction(models.Model):
    """One row per tool call — the inspectable detail behind a summary,
    and the source of the approval-needed badge count."""

    run = models.ForeignKey(
        AIEmployeeRun, on_delete=models.CASCADE, related_name='actions')
    tool_name = models.CharField(max_length=60)
    tool_input = models.JSONField(default=dict)
    result = models.TextField(blank=True)

    requires_approval = models.BooleanField(default=False, db_index=True)
    approved = models.BooleanField(null=True, blank=True)  # null = pending
    approved_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            # Drives the nav badge: pending-approval count.
            models.Index(fields=['requires_approval', 'approved']),
        ]

    def __str__(self):
        return f'{self.tool_name} (run {self.run_id})'


class AIEmployeeTask(models.Model):
    """A manually-assigned instruction, picked up on the next run."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]

    employee = models.ForeignKey(
        AIEmployee, on_delete=models.CASCADE, related_name='tasks')
    instruction = models.TextField()
    status = models.CharField(
        max_length=20, default='pending', choices=STATUS_CHOICES,
        db_index=True)
    created_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    result = models.TextField(blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.employee.name}: {self.instruction[:60]} ({self.status})'
