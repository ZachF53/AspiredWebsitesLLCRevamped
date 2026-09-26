"""
Sept 2026 buyer-persona review follow-up: the Pay-in-Full pricing card
carried exactly one feature bullet ("Custom-coded, not a template") on a
$2,000 purchase card, and the installment card only two. Fills both
cards out with the build facts the owner confirmed (four main pages plus
up to six service pages, copy included, timeline, revisions, ownership).

Guarded: bullets are only ADDED, each only if no feature with the same
text already exists on that tier, so an operator's edits in the pricing
admin are never overwritten and a re-run is a no-op. seed_pricing carries
the same lists for fresh installs.
"""
from django.db import migrations

BULLETS = {
    'hvac-build-full': [
        'Four main pages plus up to six service pages',
        'Copy written for you from your notes and photos',
        'Live in four to six weeks',
        'Two rounds of revisions included',
        'Domain registered in your name from day one',
        'Paid in full at signing: the site is yours at launch',
    ],
    'hvac-build-installment': [
        'Same build: four main pages plus up to six service pages',
        'First payment at signing; the site can launch while payments continue',
        'Ownership transfers to you at payment 24',
    ],
}


def forwards(apps, schema_editor):
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    TierFeature = apps.get_model('billing', 'TierFeature')
    for slug, bullets in BULLETS.items():
        tier = ServiceTier.objects.filter(slug=slug).first()
        if tier is None:
            continue
        existing = set(
            TierFeature.objects.filter(tier=tier).values_list('text', flat=True))
        next_order = (
            TierFeature.objects.filter(tier=tier)
            .order_by('-sort_order').values_list('sort_order', flat=True)
            .first() or 0
        )
        for text in bullets:
            if text in existing:
                continue
            next_order += 1
            TierFeature.objects.create(tier=tier, text=text, sort_order=next_order)


def backwards(apps, schema_editor):
    TierFeature = apps.get_model('billing', 'TierFeature')
    for slug, bullets in BULLETS.items():
        TierFeature.objects.filter(tier__slug=slug, text__in=bullets).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0011_deactivate_discontinued_tiers'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
