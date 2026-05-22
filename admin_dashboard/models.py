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
