"""Billing models — out-of-scope mini invoices + pricing tiers."""

from django.db import models

from clients.models import ClientProfile, Project, RevisionRequest
from core.models import TimestampedModel

# Pricing models live in a separate module — imported here so Django's app
# registry discovers them (billing/ is the app label).
from billing.pricing_models import (  # noqa: E402,F401
    AddonPricing,
    ServiceTier,
    TierFeature,
)


class MiniInvoice(TimestampedModel):
    """
    A small out-of-scope invoice — generated when a client exceeds their
    included revisions, or for any work outside the contract scope.
    Work is blocked until status == 'paid' (CLAUDE.md scope-creep rule).
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='mini_invoices',
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True,
        related_name='mini_invoices',
    )
    # Phase A — out-of-scope work is per-build; account FK kept so
    # Stripe Customer resolution is unambiguous on legacy rows.
    account_new = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        null=True, blank=True, related_name='mini_invoices_new',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        null=True, blank=True, related_name='mini_invoices_new',
    )
    revision = models.ForeignKey(
        RevisionRequest, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mini_invoices',
    )
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    hours = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    stripe_invoice_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Mini Invoice'
        verbose_name_plural = 'Mini Invoices'

    def __str__(self):
        return f'{self.client.firm_name}: {self.description} ({self.status})'
