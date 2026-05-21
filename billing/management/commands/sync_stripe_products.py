"""
sync_stripe_products — create Stripe Products + Prices for any active
ServiceTier that doesn't yet have a stripe_price_id, and write the new IDs
back to the database.

    python manage.py sync_stripe_products
    python manage.py sync_stripe_products --dry-run
"""

import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from billing.pricing_models import ServiceTier


class Command(BaseCommand):
    help = 'Create Stripe Products/Prices for active tiers missing a Price ID.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be created without calling Stripe.',
        )

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']
        tiers = list(
            ServiceTier.objects.filter(is_active=True, stripe_price_id='')
        )

        if not tiers:
            self.stdout.write(
                'All active tiers already have a Stripe Price ID — nothing to do.'
            )
            return

        if not dry_run and not settings.STRIPE_SECRET_KEY:
            self.stderr.write(self.style.ERROR(
                'Stripe not configured. Add STRIPE_SECRET_KEY to .env first.'
            ))
            return

        if dry_run:
            self.stdout.write('DRY RUN — no Stripe calls will be made.\n')

        import stripe
        if not dry_run:
            stripe.api_key = settings.STRIPE_SECRET_KEY

        synced = 0
        for tier in tiers:
            kind = (
                f'recurring/{tier.billing_interval or "month"}'
                if tier.is_recurring else 'one-time'
            )

            if dry_run:
                self.stdout.write(
                    f'  would create: {tier.name} '
                    f'({tier.get_price_display()}) [{kind}]'
                )
                continue

            try:
                product = stripe.Product.create(
                    name=tier.name,
                    description=tier.description or tier.name,
                    metadata={
                        'tier_slug': tier.slug,
                        'category': tier.category,
                        'aspired_tier_id': str(tier.id),
                    },
                )
                price_kwargs = {
                    'product': product.id,
                    'unit_amount': int(tier.price * 100),
                    'currency': 'usd',
                    'metadata': {'tier_slug': tier.slug},
                }
                if tier.is_recurring:
                    price_kwargs['recurring'] = {
                        'interval': tier.billing_interval or 'month',
                    }
                price = stripe.Price.create(**price_kwargs)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f'  x failed: {tier.name} — {exc}'
                ))
                continue

            tier.stripe_price_id = price.id
            tier.stripe_product_id = product.id
            tier.save(update_fields=[
                'stripe_price_id', 'stripe_product_id', 'updated_at',
            ])
            synced += 1
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ Created: {tier.name} ({tier.get_price_display()}) '
                f'→ {price.id}'
            ))

        self.stdout.write('')
        if dry_run:
            self.stdout.write(
                f'Dry run — {len(tiers)} tier(s) would be synced to Stripe.'
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Done — {synced} tiers synced to Stripe'
            ))
            self.stdout.write(
                "Run this command again anytime to sync new tiers that don't "
                "have a Price ID yet."
            )
