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
