from django.db import models


class AuditLead(models.Model):
    """
    Captures a website audit run + (optionally) the email address of someone
    who asked for the full report. The URL is only persisted when the visitor
    opts in by submitting their email on the results page.
    """

    url = models.URLField(max_length=500)
    performance_score = models.PositiveSmallIntegerField()
    seo_score = models.PositiveSmallIntegerField()
    best_practices_score = models.PositiveSmallIntegerField()
    accessibility_score = models.PositiveSmallIntegerField()
    issues = models.JSONField(default=list, blank=True)
    email = models.EmailField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Audit Lead'
        verbose_name_plural = 'Audit Leads'

    def __str__(self):
        return f'{self.url} — perf {self.performance_score}'

    @property
    def average_score(self):
        return round(
            (self.performance_score + self.seo_score
             + self.best_practices_score + self.accessibility_score) / 4
        )
