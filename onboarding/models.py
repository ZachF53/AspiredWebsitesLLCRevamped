# Re-export SetupTodo and helpers so they're discovered by Django's
# app-loading scan (models.py is the canonical file Django checks).
from onboarding.todo_models import (  # noqa: F401
    SetupTodo, build_todos_from_onboarding, TASK_TYPE_CHOICES,
)
from onboarding.password_models import PasswordSetupToken  # noqa: F401
from onboarding.question_models import (  # noqa: F401
    OnboardingSectionDef, OnboardingQuestionDef,
)


"""
Onboarding wizard models.

`Onboarding` is one row per (User, product_type) — created when a
customer purchases a product (or admin marks them eligible for one).
`OnboardingResponse` is one row per (Onboarding, question_key) — the
operator's answer, or a marker that they explicitly skipped it.

The question registry lives in code (``onboarding/registry.py``), NOT
in the database — the wizard engine just iterates the registry per
product_type and renders the matching `OnboardingResponse` rows. This
keeps editing the question set a one-line code change instead of a
migration.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


PRODUCT_TYPE_CHOICES = [
    ('maintenance',    'Maintenance'),
    ('social_media',   'Social Media'),
    ('website_design', 'Website Design'),
]


class Onboarding(models.Model):
    """
    One per (User, product_type, tier_slug). Tracks where the customer
    is in the wizard so a half-finished onboarding resumes exactly
    where they left off.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='onboardings',
    )
    product_type = models.CharField(
        max_length=20, choices=PRODUCT_TYPE_CHOICES,
    )
    # Matches billing.pricing_models.ServiceTier.slug — drives the
    # per-tier conditional logic in the registry (e.g. social channel
    # count, Standard/Full-only reply-policy section).
    tier_slug = models.CharField(max_length=50)

    welcome_seen = models.BooleanField(default=False)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Resume bookmark — the last section the user visited.
    last_section = models.CharField(max_length=10, blank=True)

    class Meta:
        unique_together = ('user', 'product_type', 'tier_slug')
        ordering = ['-started_at']

    def __str__(self):
        return (f'{self.user} — {self.get_product_type_display()} '
                f'({self.tier_slug})')


class OnboardingResponse(models.Model):
    """
    The customer's answer to a single onboarding question. ``skipped``
    is the explicit "Skip this question" click — distinct from a
    not-yet-answered question (which has no row at all).
    """

    onboarding = models.ForeignKey(
        Onboarding, on_delete=models.CASCADE, related_name='responses',
    )
    question_key = models.CharField(max_length=80)
    value = models.TextField(blank=True)
    skipped = models.BooleanField(default=False)
    saved_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('onboarding', 'question_key')
        ordering = ['question_key']

    def __str__(self):
        state = 'SKIPPED' if self.skipped else (
            f'"{self.value[:30]}..."' if len(self.value) > 30 else
            f'"{self.value}"')
        return f'{self.question_key} = {state}'
