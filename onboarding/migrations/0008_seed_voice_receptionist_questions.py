"""
Seed the AI Voice Receptionist wizard (sections V1–V5) into the DB-backed
registry.

Same reasoning as ``0006`` for the Review System: optional in the strict
sense, because ``registry._db_sections`` returns None for a product with
no rows and the wizard falls back to ``registry._VOICE_RECEPTIONIST``.
Done anyway so the product works the moment the code deploys without
anyone remembering a management command, and so the questions are
editable in the admin question editor, which only reads the DB registry.

Depends on 0007, which widens the ``product_type`` choices — seeding
rows with a value the field does not yet accept would be rejected by the
admin editor's own validation later even though the DB would store it.

Delegates to the real seeder rather than restating 40 question dicts, so
the registry stays the single definition.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    # Uses the real model classes and the registry, not the historical
    # ones — safe because this writes registry rows only and never
    # touches a client's answers. Same note as 0006.
    try:
        from onboarding import registry as reg
        from onboarding.management.commands.seed_onboarding_questions import (
            _seed_section,
        )
    except Exception:
        return

    sections = getattr(reg, '_VOICE_RECEPTIONIST', None)
    if not sections:
        return
    for index, section in enumerate(sections):
        _seed_section('voice_receptionist', section, index)


def backwards(apps, schema_editor):
    OnboardingSectionDef = apps.get_model(
        'onboarding', 'OnboardingSectionDef')
    # Questions cascade with their section.
    OnboardingSectionDef.objects.filter(
        product_type='voice_receptionist').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('onboarding', '0007_alter_onboarding_product_type_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
