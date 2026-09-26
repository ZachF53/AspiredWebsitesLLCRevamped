"""
Sept 2026 review follow-up: gives multi-location work a published
anchor price ("from $500 per location") instead of only "quoted on the
call". The pricing FAQ reads this row, so the number lives in the DB
like every other price (CLAUDE.md: never hardcode prices).

Guarded: created only if the slug doesn't exist, so admin edits to the
price survive re-runs. seed_pricing carries the same row for fresh
installs.
"""
from decimal import Decimal

from django.db import migrations


def forwards(apps, schema_editor):
    AddonPricing = apps.get_model('billing', 'AddonPricing')
    if AddonPricing.objects.filter(slug='addon-location').exists():
        return
    AddonPricing.objects.create(
        slug='addon-location', name='Additional Location',
        price_min=Decimal('500.00'), price_max=None, unit='per location',
        description=('Location pages, review-request routing to that '
                     "branch's Google Business Profile, and per-location "
                     'reporting, added to an existing build. Always quoted '
                     'in writing before signing.'),
        is_active=True,
    )


def backwards(apps, schema_editor):
    AddonPricing = apps.get_model('billing', 'AddonPricing')
    AddonPricing.objects.filter(slug='addon-location').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0012_build_tier_feature_bullets'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
