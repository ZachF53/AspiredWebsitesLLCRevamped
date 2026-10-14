"""
Oct 2026: the review system becomes a standalone $97/month product.

Automated review generation was previously only reachable by buying the
Full Plan, which meant only reachable by buying a website build. This
creates the ServiceTier row the public pricing page, the review-automation
service page and self-checkout all read, so the offer exists on deploy
without anyone having to remember to run seed_pricing.

category='addon' (not 'maintenance') is deliberate — see the note on this
tier in billing/management/commands/seed_pricing.py. The short version:
the portal's plan chooser filters category='maintenance' and is a SWITCH
surface, so a maintenance row would let a Full Plan client switch down to
reviews-only and silently lose their hosting.

Guarded: created only if the slug doesn't exist, so a later price edit in
the admin dashboard survives a re-run. stripe_price_id is deliberately
left blank — the row is not chargeable until `sync_stripe_products` runs
(or the ID is pasted in by hand), and checkout_confirm already refuses a
tier with no Stripe price rather than taking a payment it can't bill.
"""
from decimal import Decimal

from django.db import migrations


FEATURES = [
    'Every completed job triggers a Google review request',
    'Sent by text and email while the work is still fresh',
    'Connects to ServiceTitan, Housecall Pro, Jobber and most others by '
    'webhook or Zapier',
    'Works with the website you already have, or no website at all',
    'Asks every customer the same way: no filtering, no incentives',
    'Opt-out handled automatically on every message',
    'Month-to-month: cancel anytime',
]


def forwards(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    TierFeature = apps.get_model('billing', 'TierFeature')
    if ServiceTier.objects.filter(slug='hvac-review-system').exists():
        return
    tier = ServiceTier.objects.create(
        slug='hvac-review-system', category='addon', name='Review System',
        price=Decimal('97.00'), is_recurring=True, billing_interval='month',
        tagline='The review system on its own. No website build required.',
        description=('Automated Google review requests running on the '
                     'website and job system you already have. Every '
                     'completed job triggers a text and email asking for a '
                     'review. Month-to-month, cancel anytime. Included at '
                     'no extra cost in the Full Plan.'),
        is_active=True, is_public=True, is_featured=False, sort_order=0,
    )
    for index, text in enumerate(FEATURES, start=1):
        TierFeature.objects.create(tier=tier, text=text, sort_order=index)


def backwards(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    ServiceTier.objects.filter(slug='hvac-review-system').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0013_addon_location_pricing'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
