"""Register 'Prospect' — the cold outreach agent — in the AI Employees registry.

Prospect is the first AI employee. It sources leads, researches them,
drafts outreach from approved template variants, and handles inbound
replies — all inside the §1 guardrails (pricing, approved templates,
daily spend cap).

Seeded PAUSED (``active=False``) on purpose. Turning it on is a
deliberate act by a human in the admin, matching how
``OutreachSettings.outreach_active`` defaults to False: nothing that can
email a real prospect starts itself.

Idempotent on ``slug``, so re-running will not overwrite settings changed
in the admin afterwards.
"""

from django.db import migrations


PROSPECT_SLUG = 'prospect'
PROSPECT_NAME = 'Prospect'
PROSPECT_ROLE = (
    'Finds and qualifies new business for Aspired Websites. Sources leads, '
    'researches each one, drafts cold outreach from approved template '
    'variants, and triages inbound replies. Cannot quote a price it made '
    'up, cannot put a new template angle into rotation without approval, '
    'and stops spending when the daily cap is reached.'
)


def seed_prospect(apps, schema_editor):
    AIEmployee = apps.get_model('admin_dashboard', 'AIEmployee')
    AIEmployee.objects.get_or_create(
        slug=PROSPECT_SLUG,
        defaults={
            'name': PROSPECT_NAME,
            'role_description': PROSPECT_ROLE,
            # Paused until a human switches it on.
            'active': False,
            'run_interval_minutes': 60,
            'reasoning_effort': 'medium',
        },
    )


def unseed_prospect(apps, schema_editor):
    AIEmployee = apps.get_model('admin_dashboard', 'AIEmployee')
    AIEmployee.objects.filter(slug=PROSPECT_SLUG).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('admin_dashboard', '0005_aiemployee_aiemployeerun_aiemployeetask_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_prospect, unseed_prospect),
    ]
