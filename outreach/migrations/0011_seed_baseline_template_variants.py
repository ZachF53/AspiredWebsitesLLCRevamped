"""Seed the four hardcoded step prompts as EmailTemplateVariant rows.

Lifts the ``step_brief`` dict that lived in
``outreach/sender.py::_user_prompt_for_step`` into the database verbatim,
one active variant per sequence step. That is the starting
single-variant-per-step state Prospect grows from.

Only the per-step ANGLE moves. Aspired's voice and hard constraints stay
in ``sender._system_prompt()`` in code, shared by every variant — see the
EmailTemplateVariant docstring for why.

Idempotent: keyed on (sequence_step, name), so re-running is a no-op and
it will not clobber edits made in the admin afterwards.
"""

from django.db import migrations


BASELINE_ANGLES = {
    1: (
        'STEP 1 — first touch. Introduce yourself briefly. If the facts '
        'above include a website, PageSpeed score, HTTPS issue or '
        'location, reference exactly one of them. If they include none '
        'of those, write the email anyway without any specific '
        'observation — do not ask for more data and do not invent a '
        'detail. End with a single low-friction question (reply '
        'yes/no). Do NOT pitch services in the first email.'),
    2: (
        'STEP 2 — follow up to a step-1 email that received no reply. '
        'Mention you reached out previously. Offer one concrete '
        'value-add observation (a specific improvement you would make). '
        'Keep it shorter than step 1.'),
    3: (
        'STEP 3 — second follow-up. Acknowledge they may be busy. Offer '
        'one resource or a 15-minute call. Brief — 3-4 sentences max.'),
    4: (
        'STEP 4 — break-up email. Brief and warm. Say this is the last '
        'email, leave the door open for them to reach out later.'),
}

VARIANT_NAME = 'Baseline'


def seed_variants(apps, schema_editor):
    EmailTemplateVariant = apps.get_model('outreach', 'EmailTemplateVariant')
    for step, angle in BASELINE_ANGLES.items():
        EmailTemplateVariant.objects.get_or_create(
            sequence_step=step,
            name=VARIANT_NAME,
            defaults={
                'angle_instructions': angle,
                # The one place a variant is born active: this is the copy
                # that has already been sending. Agent-proposed variants
                # default to inactive and need a human to flip them.
                'active': True,
                'proposed_by': 'human',
            },
        )


def unseed_variants(apps, schema_editor):
    EmailTemplateVariant = apps.get_model('outreach', 'EmailTemplateVariant')
    EmailTemplateVariant.objects.filter(
        name=VARIANT_NAME, sequence_step__in=BASELINE_ANGLES,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('outreach', '0010_emailtemplatevariant_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_variants, unseed_variants),
    ]
