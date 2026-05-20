from django.db import models


class Lead(models.Model):
    """Inbound lead from the public contact form."""

    BUSINESS_TYPE_CHOICES = [
        ('law_firm', 'Law Firm'),
        ('restaurant', 'Restaurant'),
        ('contractor', 'Contractor'),
        ('retail', 'Retail'),
        ('healthcare', 'Healthcare'),
        ('technology', 'Technology'),
        ('other', 'Other'),
    ]

    SOURCE_CHOICES = [
        ('google', 'Google Search'),
        ('referral', 'Referral'),
        ('social', 'Social Media'),
        ('cold_email', 'Cold Email'),
        ('other', 'Other'),
    ]

    STATUS_CHOICES = [
        ('new', 'New'),
        ('contacted', 'Contacted'),
        ('qualified', 'Qualified'),
        ('won', 'Won'),
        ('lost', 'Lost'),
    ]

    name = models.CharField(max_length=120)
    business_name = models.CharField(max_length=200)
    business_type = models.CharField(max_length=20, choices=BUSINESS_TYPE_CHOICES)
    phone = models.CharField(max_length=30)
    email = models.EmailField()
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, blank=True)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Lead'
        verbose_name_plural = 'Leads'

    def __str__(self):
        return f'{self.business_name} — {self.name} ({self.get_status_display()})'
