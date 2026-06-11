"""
DB-backed onboarding question registry.

The wizard's sections + questions used to live as Python dicts in
``onboarding/registry.py``. They now live here so the operator can
add / edit / remove / reorder them from the admin dashboard. The
registry helpers read these models (falling back to the Python
definitions only if the tables are empty, e.g. before the seed runs).

Conditional visibility (which used to be CONDITIONAL_RULES) is captured
as fields on the section: per-tier visibility, the hosting-move-over
gate, the completed-intake skip, and the social channel-template flag.
"""

from django.db import models

PRODUCT_TYPE_CHOICES = [
    ('maintenance', 'Maintenance'),
    ('social_media', 'Social Media'),
    ('website_design', 'Website Design'),
]

QUESTION_TYPE_CHOICES = [
    ('text', 'Short text'),
    ('textarea', 'Long text'),
    ('select', 'Dropdown'),
    ('bool', 'Yes / No'),
    ('cred_access', 'Credential access'),
]


class OnboardingSectionDef(models.Model):
    """One section (a page/tab) of an onboarding wizard."""

    product_type = models.CharField(
        max_length=20, choices=PRODUCT_TYPE_CHOICES, db_index=True)
    key = models.CharField(
        max_length=20,
        help_text='Stable code, e.g. M1 / S2. Unique per product type.')
    title = models.CharField(max_length=160)
    intro = models.TextField(
        blank=True, help_text='Optional blurb under the section title.')
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # ── Conditional visibility (was CONDITIONAL_RULES) ──
    tier_visibility = models.JSONField(
        default=list, blank=True,
        help_text='List of tier slugs this section shows for. Empty = all '
                  'tiers. e.g. ["social-standard","social-full"].')
    requires_hosting_moveover = models.BooleanField(
        default=False,
        help_text='Only show if the client bought the hosting move-over '
                  '(maintenance migration section).')
    skip_if_completed_intake = models.BooleanField(
        default=False,
        help_text='Hide if the client already completed the website intake '
                  '(we already have these answers).')
    is_channel_template = models.BooleanField(
        default=False,
        help_text='Social only — repeat this section\'s questions once per '
                  'channel in the tier (keys become channel_<n>_<key>).')

    class Meta:
        ordering = ['product_type', 'sort_order', 'key']
        unique_together = ('product_type', 'key')
        verbose_name = 'Onboarding Section'

    def __str__(self):
        return f'{self.get_product_type_display()} · {self.key} — {self.title}'


class OnboardingQuestionDef(models.Model):
    """A single question within a section."""

    section = models.ForeignKey(
        OnboardingSectionDef, on_delete=models.CASCADE,
        related_name='questions')
    key = models.CharField(
        max_length=80,
        help_text='Stable code that persists answers. For channel-template '
                  'sections use the base key (e.g. "platform").')
    label = models.CharField(max_length=300)
    qtype = models.CharField(
        max_length=20, choices=QUESTION_TYPE_CHOICES, default='text')
    help = models.CharField(max_length=400, blank=True)
    placeholder = models.CharField(max_length=200, blank=True)
    required = models.BooleanField(default=True)
    skip_allowed = models.BooleanField(default=True)
    rows = models.IntegerField(
        null=True, blank=True, help_text='Rows for a long-text box.')
    choices = models.JSONField(
        default=list, blank=True,
        help_text='Dropdown options as [["value","Label"], ...].')
    cred_category = models.CharField(max_length=40, blank=True)
    cred_type = models.CharField(max_length=60, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sort_order', 'id']
        verbose_name = 'Onboarding Question'

    def __str__(self):
        return f'{self.section.key} · {self.key}'
