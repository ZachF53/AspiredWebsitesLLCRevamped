"""Plan M-2.10: the two Full Plan tiers were "Full Plan" and "Full Plan:
Build Paid in Full", indistinguishable when the cards stack on a phone.
Renamed at the source so the pricing cards, checkout and contracts all
agree. Only renames a row still carrying its old name."""

from django.db import migrations

RENAMES = {
    'hvac-full-plan': ('Full Plan', 'Full Plan: Build Financed'),
    'hvac-plan-paid-in-full': ('Full Plan: Build Paid in Full',
                               'Full Plan: Build Paid Upfront'),
}


def forwards(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    for slug, (old, new) in RENAMES.items():
        ServiceTier.objects.filter(slug=slug, name=old).update(name=new)


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0016_sept2026_content_alignment'),
        ('billing', '0011_deactivate_discontinued_tiers'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
