"""
Seed the Review System wizard (sections R1–R5) into the DB-backed
registry.

Strictly speaking optional: ``registry._db_sections`` filters by
``product_type`` and returns None when that product has no rows, so an
unseeded ``review_system`` falls back to the Python definitions in
``registry._REVIEW_SYSTEM`` and the wizard works either way.

It exists anyway, for the same reason ``billing/0014`` exists alongside
``seed_pricing``: the product has to work the moment the code deploys,
without anyone remembering to run a management command. It also makes
the questions editable in the admin question editor, which only reads
the DB registry.

Delegates to the real seeder rather than restating 40 question dicts —
the registry is the single definition, and a copy here would drift the
first time a label changed. Guarded and idempotent (the seeder itself
uses ``update_or_create``), so a re-run is safe and an operator's label
edits to OTHER products are untouched.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    # The seeder uses the real model classes (and the registry), not the
    # historical ones. That is safe here because this migration only
    # writes registry rows and never touches user answers, and because
    # the fields it writes all exist as of 0005. If a later migration
    # changes OnboardingQuestionDef's shape, this one is already applied
    # everywhere and will not run again.
    try:
        from onboarding import registry as reg
        from onboarding.management.commands.seed_onboarding_questions import (
            _seed_section,
        )
    except Exception:
        return

    sections = getattr(reg, '_REVIEW_SYSTEM', None)
    if not sections:
        return
    for index, section in enumerate(sections):
        _seed_section('review_system', section, index)


def backwards(apps, schema_editor):
    OnboardingSectionDef = apps.get_model('onboarding', 'OnboardingSectionDef')
    # Questions cascade with their section.
    OnboardingSectionDef.objects.filter(product_type='review_system').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('onboarding', '0005_onboardingquestiondef_default_value_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
