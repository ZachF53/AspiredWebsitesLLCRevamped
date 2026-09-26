"""Retire the pre-HVAC tiers (Sept 2026).

The business now sells one offer — the HVAC website build (in full or in
24 installments), the Full Plan and Hosting + Security. Every tier below
is discontinued: it must stop being billable (is_active=False) and stop
showing anywhere public (is_public=False).

Rows are NOT deleted. Existing clients' MaintenancePlan / Website rows
still reference these slugs for display, and their live Stripe prices
keep billing until each client moves over. Idempotent — a no-op on rows
that are already inactive, and on databases that never had the row.
"""

from django.db import migrations

DISCONTINUED_SLUGS = [
    'website-essential',
    'website-premium',
    'maintenance-essentials',
    'maintenance-growth',
    'maintenance-dominant',
    'social-basic',
    'social-standard',
    'social-full',
    'hosting-annual',
    'domain-law',
]


def deactivate(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    ServiceTier.objects.filter(slug__in=DISCONTINUED_SLUGS).update(
        is_active=False, is_public=False)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0010_striperevenuesync'),
    ]

    operations = [
        # Deliberately not reversible into "active": re-activating a
        # discontinued tier is a business decision, made in the pricing
        # admin, not a side effect of rolling back a migration.
        migrations.RunPython(deactivate, migrations.RunPython.noop),
    ]
