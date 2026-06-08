"""
Data migration — backfill MaintenancePlan + Droplet rows from existing
ClientProfile + Website data.

Idempotent — uses get_or_create on natural keys. Existing
ClientProfile.package / maintenance_active fields are NOT touched;
the new rows mirror them so new code paths can read from
MaintenancePlan / Droplet without breaking legacy reads.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    ClientProfile = apps.get_model('clients', 'ClientProfile')
    Account = apps.get_model('clients', 'Account')
    Website = apps.get_model('clients', 'Website')
    MaintenancePlan = apps.get_model('clients', 'MaintenancePlan')
    Droplet = apps.get_model('clients', 'Droplet')

    package_to_tier = {
        'maintenance_essentials': 'maintenance-essentials',
        'maintenance_growth':     'maintenance-growth',
        'maintenance_dominant':   'maintenance-dominant',
    }
    for cp in ClientProfile.objects.exclude(package='').iterator():
        tier = package_to_tier.get(cp.package)
        if not tier:
            continue
        account = Account.objects.filter(
            legacy_client_profile=cp).first()
        if account is None:
            continue
        website = Website.objects.filter(account=account).first()
        MaintenancePlan.objects.get_or_create(
            account=account, tier_slug=tier,
            defaults={
                'website': website,
                'status': 'active' if cp.maintenance_active else 'paused',
                'stripe_subscription_id': cp.stripe_subscription_id or '',
                'started_at': cp.maintenance_started_at or cp.created_at,
            },
        )

    for website in Website.objects.exclude(do_droplet_id='').iterator():
        if not website.account_id:
            continue
        Droplet.objects.get_or_create(
            account=website.account,
            do_droplet_id=str(website.do_droplet_id),
            defaults={
                'website': website,
                'source': 'build',
                'status': 'active',
                'do_droplet_ip': getattr(website, 'do_droplet_ip', None),
                'do_region': getattr(website, 'do_region', 'nyc1') or 'nyc1',
                'provisioned_at': website.created_at,
            },
        )


def backwards(apps, schema_editor):
    MaintenancePlan = apps.get_model('clients', 'MaintenancePlan')
    Droplet = apps.get_model('clients', 'Droplet')
    MaintenancePlan.objects.all().delete()
    Droplet.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0029_service_models'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
