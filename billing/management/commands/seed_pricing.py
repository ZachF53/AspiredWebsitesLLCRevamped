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
        'is_active': False,
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
        'is_active': False,
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
    # Retired (Sept 2026) — superseded by the HVAC-era hvac-full-plan /
    # hvac-plan-paid-in-full below. is_active=False so Add Plan and any
    # other operator-selection surface stop offering them; left in the
    # DB (not deleted) since any already-signed client still referencing
    # one of these slugs on their MaintenancePlan.tier_slug must keep
    # displaying correctly (tier_slug is a static choices field, not FK'd
    # to ServiceTier, so this doesn't touch existing display at all).
    {
        'slug': 'maintenance-essentials', 'category': 'maintenance',
        'name': 'Essentials', 'price': Decimal('299.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 1, 'is_featured': False, 'is_active': False,
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
        'sort_order': 2, 'is_featured': True, 'is_active': False,
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
        'sort_order': 3, 'is_featured': False, 'is_active': False,
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
        'is_active': False,
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
        'is_active': False,
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
        'is_active': False,
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
        'is_active': False,
        'env': 'STRIPE_PRICE_HOSTING',
        'features': [
            'Your own dedicated server — not shared hosting',
            'Uptime monitoring 24/7',
            'Monthly backups verified',
            'SSL certificate management',
        ],
    },
    # ─── HVAC pricing (Sept 2026 repositioning) ───
    # Additive only — none of the tiers above are touched. These are new
    # rows for the public pricing page; existing clients' build/maintenance/
    # social/hosting tiers (and their live Stripe Price IDs) are untouched.
    # `stripe_price_id` stays blank until Zach runs sync_stripe_products or
    # sets one by hand — see the Change 8 billing note in pricing.html and
    # the deliverable report for what checkout wiring these still need.
    {
        'slug': 'hvac-build-full', 'category': 'website_build',
        'name': 'Website Build: Pay in Full', 'price': Decimal('2000.00'),
        'is_recurring': False, 'billing_interval': '',
        'sort_order': 20, 'is_featured': False,
        'env': 'STRIPE_PRICE_HVAC_BUILD_FULL',
        'tagline': 'Own it outright from day one.',
        'description': ('Aspired Websites LLC retains ownership of the '
                        'website until paid in full.'),
        'features': [
            'Custom-coded, not a template',
            'Four main pages plus up to six service pages',
            'Copy written for you from your notes and photos',
            'Live in four to six weeks',
            'Two rounds of revisions included',
            'Domain registered in your name from day one',
            'Paid in full at signing: the site is yours at launch',
        ],
    },
    {
        'slug': 'hvac-build-installment', 'category': 'website_build',
        'name': 'Website Build: 24-Month Installment',
        'price': Decimal('105.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'price_display': '$105/mo',
        'sort_order': 21, 'is_featured': False,
        'env': 'STRIPE_PRICE_HVAC_BUILD_INSTALLMENT',
        'tagline': '$2,520 total — same build, spread out.',
        'description': ('Aspired Websites LLC retains ownership of the '
                        'website until paid in full.'),
        'features': [
            'Custom-coded, not a template',
            '24 payments of $105 ($2,520 total)',
            'Same build: four main pages plus up to six service pages',
            'First payment at signing; the site can launch while payments continue',
            'Ownership transfers to you at payment 24',
        ],
    },
    {
        'slug': 'hvac-full-plan', 'category': 'maintenance',
        'name': 'Full Plan: Build Financed', 'price': Decimal('250.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 0, 'is_featured': True,
        'env': 'STRIPE_PRICE_HVAC_FULL_PLAN',
        'tagline': 'Build payment plus hosting, maintenance, and the '
                   'automated review system — bundled into one plan.',
        'description': ('Includes the $105/month build payment plus '
                        '$145/month for hosting, maintenance, unlimited '
                        'content updates, security patching, and the '
                        'automated review system. After the build is '
                        'paid off at month 24, the rate drops '
                        'automatically to $145/month. Month-to-month '
                        'after that — cancel anytime.'),
        'features': [
            'Includes the $105/mo build payment',
            'Hosting, maintenance, and unlimited content updates',
            'Security patching included',
            'Automated review generation included',
            'Drops to $145/mo automatically after month 24',
            'Month-to-month after the build term: cancel anytime',
            'Monthly security report: an automated scan of your site, '
            'summarized in a one-page PDF emailed to you on the 1st.',
        ],
    },
    {
        'slug': 'hvac-plan-paid-in-full', 'category': 'maintenance',
        'name': 'Full Plan: Build Paid Upfront', 'price': Decimal('145.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 1, 'is_featured': False,
        'env': 'STRIPE_PRICE_HVAC_PLAN_PAID_IN_FULL',
        'tagline': 'For clients who paid the $2,000 build upfront — '
                   'no build payment folded into the monthly rate.',
        'description': ('Hosting, maintenance, unlimited content '
                        'updates, security patching, and the automated '
                        'review system. Month-to-month, cancel anytime.'),
        'features': [
            'Hosting, maintenance, and unlimited content updates',
            'Security patching included',
            'Automated review generation included',
            'Month-to-month from day one: cancel anytime',
            'Monthly security report: an automated scan of your site, '
            'summarized in a one-page PDF emailed to you on the 1st.',
        ],
    },
    {
        'slug': 'hvac-hosting-security', 'category': 'hosting',
        'name': 'Hosting + Security Only', 'price': Decimal('45.00'),
        'is_recurring': True, 'billing_interval': 'month',
        'sort_order': 20, 'is_featured': False,
        'env': 'STRIPE_PRICE_HVAC_HOSTING_SECURITY',
        'tagline': 'Server and OS patching, SSL renewal, security '
                   'updates. Forms stay live.',
        'description': ('No content edits, no code changes, no review '
                        'system.'),
        'features': [
            'Server and OS patching',
            'SSL certificate renewal',
            'Application and dependency security updates',
            'Forms stay live',
            'Monthly security report: an automated scan of your site, '
            'summarized in a one-page PDF emailed to you on the 1st.',
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
            'Covers .com, .net, and .org',
        ],
    },
    {
        'slug': 'domain-law', 'category': 'addon',
        'name': 'Domain Registration — Attorney TLDs',
        'price': Decimal('175.00'),
        'is_recurring': True, 'billing_interval': 'year',
        'sort_order': 11, 'is_featured': False,
        'is_active': False,
        'env': 'STRIPE_PRICE_DOMAIN_LAW',
        'features': [
            'Premium attorney-niche TLDs — .law, .legal, .attorney',
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
        'slug': 'addon-location', 'name': 'Additional Location',
        'price_min': Decimal('500.00'), 'price_max': None,
        'unit': 'per location',
        'description': ('Location pages, review-request routing to that '
                        'branch\'s Google Business Profile, and per-location '
                        'reporting, added to an existing build. Always '
                        'quoted in writing before signing.'),
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
            defaults = {
                'category': data['category'],
                'name': data['name'],
                'price': data['price'],
                'price_display': data.get('price_display', ''),
                'tagline': data.get('tagline', ''),
                'description': data.get('description', ''),
                'is_recurring': data['is_recurring'],
                'billing_interval': data['billing_interval'],
                'stripe_price_id': preserve_id,
                'stripe_product_id': preserve_product,
                'is_active': data.get('is_active', True),
                'is_featured': data['is_featured'],
                'sort_order': data['sort_order'],
                'pages_included': data.get('pages_included'),
                'practice_areas_included': data.get('practice_areas_included'),
                'timeline_weeks': data.get('timeline_weeks'),
            }
            # Discontinued tiers (see billing migration 0011) are also
            # hidden. Active tiers leave is_public alone, so an operator's
            # choice to hide one survives a re-seed.
            if not data.get('is_active', True):
                defaults['is_public'] = False
            tier, _ = ServiceTier.objects.update_or_create(
                slug=data['slug'], defaults=defaults)
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
