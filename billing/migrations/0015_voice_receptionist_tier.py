"""
Oct 2026: the AI Voice Receptionist becomes a standalone $97/month product.

Creates the ServiceTier row the public pricing page, the new
/services/ai-receptionist/ page and self-checkout all read, so the offer
exists on deploy without anyone having to remember to run seed_pricing.

category='addon' (not 'maintenance') for the same reason as the Review
System in 0014 — the portal's plan chooser filters category='maintenance'
and is a SWITCH surface, so a maintenance row would let a Full Plan client
switch down to receptionist-only and silently lose their hosting.

The difference from the Review System, and it is the commercial point:
the Full Plan bundles reviews at no cost but DOES NOT bundle this. The
receptionist bills real per-minute vendor cost on every call, and it is
the only product sellable to a lead with a phone and no website. See
clients/contract_options.VOICE_RECEPTIONIST_SLUG.

$97 is the OPENING price — the first cohort of 10 accounts. It steps +$50
every 10 (voice.cohorts.maybe_escalate rewrites this row). The matching
VoiceCohort row is created by voice/migrations/0002, which depends on this
one, so the ladder and the tier can never disagree about the opening price.

Guarded: created only if the slug doesn't exist, so a later price edit or
an escalation survives a re-run. stripe_price_id is deliberately left
blank — the row is not chargeable until `sync_stripe_products` runs (or
the ID is pasted in by hand), and checkout_confirm already refuses a tier
with no Stripe price rather than taking a payment it can't bill.
"""
from decimal import Decimal

from django.db import migrations


FEATURES = [
    'Answers every call, day or night, or only after hours: your choice',
    'Screens spam and robocalls before your phone ever rings',
    'Takes the name, number, address and the actual problem',
    'Transfers a real customer straight to you, no phone tree',
    'Texts you the details the moment the call ends',
    '300 talk minutes a month included, then 25 cents a minute',
    'Works with the phone number you already have',
    'Month-to-month: cancel anytime',
]

TAGLINE = 'Answers your phone when you cannot. No website required.'

DESCRIPTION = (
    'An AI receptionist that answers your business line, screens the '
    'robocalls, answers the questions you get asked every day, takes the '
    'job details and texts them to you. 300 minutes of talk time a month '
    'included, then 25 cents a minute. Month-to-month, cancel anytime. '
    'This is an add-on: it is not included in any plan.'
)


def forwards(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    TierFeature = apps.get_model('billing', 'TierFeature')
    if ServiceTier.objects.filter(slug='hvac-voice-receptionist').exists():
        return
    tier = ServiceTier.objects.create(
        slug='hvac-voice-receptionist', category='addon',
        name='AI Voice Receptionist',
        price=Decimal('97.00'), is_recurring=True, billing_interval='month',
        tagline=TAGLINE, description=DESCRIPTION,
        is_active=True, is_public=True, is_featured=False, sort_order=1,
    )
    for index, text in enumerate(FEATURES, start=1):
        TierFeature.objects.create(tier=tier, text=text, sort_order=index)


def backwards(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    ServiceTier.objects.filter(slug='hvac-voice-receptionist').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0014_review_system_standalone_tier'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
