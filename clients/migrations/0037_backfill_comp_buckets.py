"""
Phase 5d — backfill the new comp_* buckets from the legacy
comp_package field. Build packages move to comp_build_package;
maintenance/moonieful packages move to comp_maintenance_package.
The legacy comp_package is left in place as a deprecation alias —
removed in a follow-up once we've confirmed no readers remain.
"""

from django.db import migrations


BUILD_SLUGS = {'essential_build', 'premium_build'}
MAINT_SLUGS = {
    'maintenance_essentials', 'maintenance_growth',
    'maintenance_dominant', 'moonieful_referred',
}


def backfill(apps, schema_editor):
    ClientProfile = apps.get_model('clients', 'ClientProfile')
    qs = ClientProfile.objects.exclude(comp_package='')
    for profile in qs.iterator():
        slug = profile.comp_package
        if slug in BUILD_SLUGS and not profile.comp_build_package:
            profile.comp_build_package = slug
        elif slug in MAINT_SLUGS and not profile.comp_maintenance_package:
            profile.comp_maintenance_package = slug
        profile.save(update_fields=[
            'comp_build_package',
            'comp_maintenance_package',
            'updated_at',
        ])


def noop(apps, schema_editor):
    """Reverse: leave the buckets in place. The deprecation alias
    still holds the value, so no data loss either way."""
    return


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0036_clientprofile_comp_build_package_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
