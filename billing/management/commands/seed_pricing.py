"""
seed_pricing — create/refresh every ServiceTier, TierFeature, and
AddonPricing row.

Idempotent: keyed on slug via update_or_create, so re-running updates rather
than duplicates. Stripe Price IDs are seeded from the STRIPE_PRICE_* env vars
(legacy reference values); the database is the source of truth thereafter.
"""

import os
import sys
from decimal import Decimal

from django.core.management.base import BaseCommand

from billing.pricing_models import AddonPricing, ServiceTier, TierFeature


# ── Seed data ────────────────────────────────────────────────────────────────

TIERS = [
    # ─── Website builds ───
    {
        'slug': 'website-essential', 'category': 'website_build',
        'name': 'Essential Build', 'price': Decimal('2500.00'),
        'is_recurring': False, 'billing_interval': '',
        'pages_included': 8, 'practice_areas_included': 5,
        'timeline_weeks': 3, 'sort_order': 1, 'is_featured': False,
        'env': 'STRIPE_PRICE_ESSENTIAL',
        'features': [
            'Up to 8 pages including up to 5 practice area pages',
            'Mobile responsive',
            'Consultation request form',
            'Google Business Profile setup',
            'Basic on-page SEO',
            'SSL + security hardened by a CISSP-certified designer',
            '2 rounds of revisions included',
            '2-week post-launch support',
        ],
    },
    {
        'slug': 'website-premium', 'category': 'website_build',
        'name': 'Premium Build', 'price': Decimal('4500.00'),
        'is_recurring': False, 'billing_interval': '',
        'pages_included': 15, 'practice_areas_included': 10,
        'timeline_weeks': 4, 'sort_order': 2, 'is_featured': True,
        'env': 'STRIPE_PRICE_PREMIUM',
        'features': [
            'Up to 15 pages including up to 10 practice area pages',
            'Everything in Essential',
            'Live chat widget',
            'Advanced lead capture forms',
            'Google Analytics + Search Console setup',
            'Schema markup for law firms',
            'Speed optimization audit post-launch',
            'Competitor analysis included',
            '2-week post-launch support',
        ],
    },
    # ─── Maintenance plans ───
    {
        'slug': 'maintenance-essentials', 'category': 'maintenance',
        'name': 'Essentials', 'price': Decimal('299.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 1, 'is_featured': False,
        'env': 'STRIPE_PRICE_ESSENTIALS',
        'features': [
            'Uptime monitoring 24/7',
            'SSL certificate management',
            'Monthly security patches and updates',
            'Up to 4 hours of content updates per month',
            'Monthly performance report',
            'Emergency response within 24 hours',
            'Monthly backup verified',
        ],
    },
    {
        'slug': 'maintenance-growth', 'category': 'maintenance',
        'name': 'Growth', 'price': Decimal('599.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 2, 'is_featured': True,
        'env': 'STRIPE_PRICE_GROWTH',
        'features': [
            'Everything in Essentials',
            'Up to 8 hours of content updates per month',
            '1 blog post per month',
            'Google Business Profile management',
            'Basic SEO monitoring and recommendations',
            'Plain-English Google Analytics summary',
            'Session recording & visual heatmaps',
            'Priority response within 8 hours',
            'Quarterly strategy call (30 minutes)',
        ],
    },
    {
        'slug': 'maintenance-dominant', 'category': 'maintenance',
        'name': 'Dominant', 'price': Decimal('1199.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 3, 'is_featured': False,
        'env': 'STRIPE_PRICE_DOMINANT',
        'features': [
            'Everything in Growth',
            'Up to 12 hours of content updates per month',
            '2 blog posts per month',
            'Full SEO management',
            'Competitor rank tracking',
            'Google Ads management (ad spend billed separately)',
            'Schema markup updates',
            'Session recording & visual heatmaps',
            'Response within 4 hours',
            'Monthly strategy call (45 minutes)',
            'Quarterly full website audit',
        ],
    },
    # ─── Social media ───
    {
        'slug': 'social-basic', 'category': 'social_media',
        'name': 'Basic', 'price': Decimal('399.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 1, 'is_featured': False,
        'env': 'STRIPE_PRICE_SOCIAL_BASIC',
        'features': [
            '3 posts per week across 2 platforms',
            'Facebook + LinkedIn included',
            'Google Business Profile posts included',
        ],
    },
    {
        'slug': 'social-standard', 'category': 'social_media',
        'name': 'Standard', 'price': Decimal('699.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 2, 'is_featured': True,
        'env': 'STRIPE_PRICE_SOCIAL_STANDARD',
        'features': [
            '5 posts per week across 3 platforms',
            'Custom graphics included',
            'Story posts included',
        ],
    },
    {
        'slug': 'social-full', 'category': 'social_media',
        'name': 'Full Management', 'price': Decimal('999.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 3, 'is_featured': False,
        'env': 'STRIPE_PRICE_SOCIAL_FULL',
        'features': [
            'Daily posting across all platforms',
            'Engagement monitoring',
            'Comment responses',
            'Monthly analytics report',
        ],
    },
    # ─── Hosting ───
    {
        'slug': 'hosting-annual', 'category': 'hosting',
        'name': 'Annual Hosting', 'price': Decimal('150.00'),
        'is_recurring': True, 'billing_interval': 'year',
        'sort_order': 1, 'is_featured': False,
        'env': 'STRIPE_PRICE_HOSTING',
        'features': [
            'Your own dedicated server — not shared hosting',
            'Uptime monitoring 24/7',
            'Monthly backups verified',
            'SSL certificate management',
        ],
    },
    # ─── Domain registrations ───
    {
        'slug': 'domain-standard', 'category': 'addon',
        'name': 'Domain Registration', 'price': Decimal('75.00'),
        'is_recurring': True, 'billing_interval': 'year',
        'sort_order': 10, 'is_featured': False,
        'env': 'STRIPE_PRICE_DOMAIN_STANDARD',
        'features': [
            'WHOIS privacy included free for life',
            'DNS management through your client portal',
            'Auto-renewal handled by Stripe (you stay in control)',
            'Cancel any time — transfer-out package sent automatically',
            'Covers .com, .net, .org, .legal, .attorney',
        ],
    },
    {
        'slug': 'domain-law', 'category': 'addon',
        'name': 'Domain Registration — .law', 'price': Decimal('175.00'),
        'is_recurring': True, 'billing_interval': 'year',
        'sort_order': 11, 'is_featured': False,
        'env': 'STRIPE_PRICE_DOMAIN_LAW',
        'features': [
            'Premium .law TLD — verified attorney TLD',
            'WHOIS privacy included free for life',
            'DNS management through your client portal',
            'Auto-renewal handled by Stripe',
            'Cancel any time — transfer-out package sent automatically',
        ],
    },
]

ADDONS = [
    {
        'slug': 'addon-practice-area', 'name': 'Additional Practice Area Page',
        'price_min': Decimal('150.00'), 'price_max': Decimal('200.00'),
        'unit': 'per page',
        'description': 'Extra practice area pages beyond the tier limit',
    },
    {
        'slug': 'addon-hourly', 'name': 'Out-of-Scope Work',
        'price_min': Decimal('85.00'), 'price_max': None,
        'unit': 'per hour',
        'description': ('Work outside the original project scope. '
                        'Invoiced before work begins.'),
    },
    {
        'slug': 'addon-session-recording',
        'name': 'Session Recording & Heatmaps',
        'price_min': Decimal('50.00'), 'price_max': None,
        'unit': 'per month',
        'description': ('Full session replay, visual heatmaps, and '
                        'scroll-depth analytics for your website. '
                        'See exactly how visitors interact with every '
                        'page. Included free in Growth and Dominant '
                        'maintenance plans.'),
        'included_in_plans': [
            'maintenance_growth',
            'maintenance_dominant',
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed/refresh all pricing tiers, features, and add-ons (idempotent).'

    def handle(self, *args, **options):
        # Allow the ✓ / ⚠ symbols to print on Windows consoles.
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        tier_count = feature_count = 0
        status_lines = []
        last_category = None

        for data in TIERS:
            env_stripe_id = os.environ.get(data['env'], '') or ''
            existing = ServiceTier.objects.filter(slug=data['slug']).first()
            # DB is the source of truth for stripe_price_id once set.
            # Only fall back to the env var on FIRST seed (when the
            # row doesn't yet exist) or when the row's id was never
            # populated. This protects sync_stripe_products' output
            # from being clobbered by a re-seed.
            preserve_id = (
                existing.stripe_price_id if existing
                and existing.stripe_price_id else env_stripe_id
            )
            preserve_product = (
                existing.stripe_product_id if existing
                and existing.stripe_product_id else ''
            )
            tier, _ = ServiceTier.objects.update_or_create(
                slug=data['slug'],
                defaults={
                    'category': data['category'],
                    'name': data['name'],
                    'price': data['price'],
                    'is_recurring': data['is_recurring'],
                    'billing_interval': data['billing_interval'],
                    'stripe_price_id': preserve_id,
                    'stripe_product_id': preserve_product,
                    'is_active': True,
                    'is_featured': data['is_featured'],
                    'sort_order': data['sort_order'],
                    'pages_included': data.get('pages_included'),
                    'practice_areas_included': data.get('practice_areas_included'),
                    'timeline_weeks': data.get('timeline_weeks'),
                },
            )
            tier_count += 1
            # Use the preserved id for the status line so re-seeds
            # accurately reflect what's actually in the DB.
            stripe_id = preserve_id

            # Rebuild features so re-running stays clean.
            tier.features.all().delete()
            for index, text in enumerate(data['features'], start=1):
                TierFeature.objects.create(tier=tier, text=text, sort_order=index)
                feature_count += 1

            if data['category'] != last_category:
                status_lines.append(f'\n{tier.get_category_display()}:')
                last_category = data['category']
            mark = '✓' if stripe_id else '⚠'
            detail = stripe_id if stripe_id else 'blank — add later'
            status_lines.append(f'  {mark} {data["slug"]:<24} {detail}')

        addon_count = 0
        for data in ADDONS:
            AddonPricing.objects.update_or_create(
                slug=data['slug'],
                defaults={
                    'name': data['name'],
                    'description': data['description'],
                    'price_min': data['price_min'],
                    'price_max': data['price_max'],
                    'unit': data['unit'],
                    'is_active': True,
                    'included_in_plans': data.get(
                        'included_in_plans', []),
                },
            )
            addon_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {tier_count} tiers, {feature_count} features, '
            f'{addon_count} addons'
        ))
        for line in status_lines:
            self.stdout.write(line)
