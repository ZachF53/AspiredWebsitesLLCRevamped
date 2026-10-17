"""
Open the first rung of the receptionist price ladder.

``voice.cohorts.current_cohort(create=True)`` can self-heal a missing
ladder, but relying on that would mean the opening cohort is created
lazily by whichever request happens to touch it first — and if that
request is an escalation check or a public page render, the price it
reads could differ from the price the tier was seeded with.

Opening it here makes the ladder exist the moment the deploy finishes,
with the same price as the tier from billing/0015 (which this migration
depends on, so the two can never disagree about the opening price).

Guarded: does nothing if any cohort already exists, so a re-run on a
database that has already escalated to $147 cannot re-open a $97 rung.
"""
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


OPENING_PRICE = Decimal('97.00')
CAPACITY = 10


def forwards(apps, schema_editor):
    VoiceCohort = apps.get_model('voice', 'VoiceCohort')
    if VoiceCohort.objects.exists():
        return
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    tier = ServiceTier.objects.filter(
        slug='hvac-voice-receptionist').first()
    VoiceCohort.objects.create(
        sequence=1,
        price=tier.price if tier is not None else OPENING_PRICE,
        # Mirrors the tier, which billing/0015 leaves blank on purpose —
        # sync_stripe_products fills both in.
        stripe_price_id=tier.stripe_price_id if tier is not None else '',
        capacity=CAPACITY,
        is_current=True,
        opened_at=timezone.now(),
    )


def backwards(apps, schema_editor):
    VoiceCohort = apps.get_model('voice', 'VoiceCohort')
    VoiceCohort.objects.filter(sequence=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('voice', '0001_initial'),
        ('billing', '0015_voice_receptionist_tier'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
