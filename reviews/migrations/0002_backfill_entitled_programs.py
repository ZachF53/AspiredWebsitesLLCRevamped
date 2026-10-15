"""
Create a ReviewProgram for every account already entitled to reviews.

Without this, every existing Full Plan client opens the portal and is
offered "Sign up — $97/mo" for a product their plan already includes.
``reviews.entitlement.review_entitlement`` reports ``has_access=True``
for them from day one, so the only thing missing is the config row the
hub and the admin queue hang off.

The slug and package tuples are inlined as literals rather than imported
from ``clients.contract_options``. Migrations must not depend on app code
that can change: if those constants are renamed or re-valued later, this
migration has to keep describing what was true when it ran.

Idempotent — skips any account that already has a program — so a re-run
after a partial failure is safe.
"""

import uuid

from django.db import migrations


# Frozen at 2026-10. See clients/contract_options.py for the live values.
FULL_PLAN_SLUGS = ('hvac-full-plan', 'hvac-plan-paid-in-full')
REVIEWS_BUNDLED_PACKAGES = ('hvac_full_plan', 'hvac_plan_paid_in_full')
SERVED_STATUSES = ('active', 'awaiting_payment', 'cancelled')


def forwards(apps, schema_editor):
    Account = apps.get_model('clients', 'Account')
    MaintenancePlan = apps.get_model('clients', 'MaintenancePlan')
    Website = apps.get_model('clients', 'Website')
    ReviewProgram = apps.get_model('reviews', 'ReviewProgram')

    entitled = set(
        MaintenancePlan.objects
        .filter(tier_slug__in=FULL_PLAN_SLUGS, status__in=SERVED_STATUSES)
        .values_list('account_id', flat=True)
    )
    entitled |= set(
        Website.objects
        .filter(package__in=REVIEWS_BUNDLED_PACKAGES)
        .values_list('account_id', flat=True)
    )
    entitled |= set(
        Account.objects
        .filter(comp_maintenance_package__in=REVIEWS_BUNDLED_PACKAGES)
        .values_list('id', flat=True)
    )
    entitled.discard(None)
    if not entitled:
        return

    already = set(
        ReviewProgram.objects
        .filter(account_id__in=entitled)
        .values_list('account_id', flat=True)
    )

    rows = []
    for account_id in sorted(entitled - already, key=str):
        rows.append(ReviewProgram(
            account_id=account_id,
            setup_status='awaiting_onboarding',
            public_key=uuid.uuid4(),
        ))
    if rows:
        ReviewProgram.objects.bulk_create(rows)


def backwards(apps, schema_editor):
    # Only remove rows that were never configured, so a reverse can't
    # destroy an operator's pasted GHL credentials or a client's wording.
    ReviewProgram = apps.get_model('reviews', 'ReviewProgram')
    (ReviewProgram.objects
     .filter(setup_status='awaiting_onboarding', ghl_location_id='',
             google_review_url='', legal_business_name='')
     .delete())


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0001_initial'),
        ('clients', '0077_legacy_profile_phone'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
