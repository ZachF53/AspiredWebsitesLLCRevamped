"""Moonieful owns intake for the clients it refers, so a synced website
should never sit in `pending_intake` (which locks the client portal onto
Aspired's own intake form and lists the site as "pending intake" on the
dashboard). sync/handlers.py now sets this on create; this backfills the
sites synced before that fix. No droplet provisioning, no email — the same
fields the admin override in clients/services.py flips."""

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Website = apps.get_model('clients', 'Website')
    IntakeResponse = apps.get_model('clients', 'IntakeResponse')
    sites = Website.objects.filter(
        moonieful_referred=True, onboarding_status='pending_intake')
    now = timezone.now()
    for site in sites:
        site.onboarding_status = 'intake_complete'
        site.save(update_fields=['onboarding_status'])
        IntakeResponse.objects.filter(
            website_new=site, completed=False,
        ).update(completed=True, completed_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0067_clientdocument_category_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
