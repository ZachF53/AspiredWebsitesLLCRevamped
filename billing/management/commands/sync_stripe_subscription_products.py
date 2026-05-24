"""
Bootstrap the Stripe Product + Price objects for recurring
subscriptions (hosting first; domain registration to follow).

Idempotent — looks up existing Products by metadata key
`aspired_subscription_kind` before creating; if a matching Product
exists, reuses it. If multiple Prices are attached, picks the most
recently created active one.

Usage:
    python manage.py sync_stripe_subscription_products

Prints the Price IDs the operator needs to add to .env:
    STRIPE_PRICE_HOSTING_YEARLY=price_xxxxxxxx
"""

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand


HOSTING_KIND = 'hosting_yearly'
HOSTING_NAME = 'Aspired Websites — Annual Hosting'
HOSTING_DESCRIPTION = (
    'Annual website hosting on Aspired Websites managed '
    'DigitalOcean infrastructure.'
)
HOSTING_AMOUNT_CENTS = 15000   # $150.00
HOSTING_INTERVAL = 'year'


class Command(BaseCommand):
    help = (
        'Create or look up the Stripe Product + Price for each '
        'recurring subscription. Idempotent.')

    def handle(self, *args, **options):
        if not settings.STRIPE_SECRET_KEY:
            self.stderr.write(self.style.ERROR(
                'STRIPE_SECRET_KEY is not set in .env — cannot '
                'talk to Stripe.'))
            return
        stripe.api_key = settings.STRIPE_SECRET_KEY

        self.stdout.write('Syncing subscription products …\n')

        hosting_price_id = self._sync_one(
            kind=HOSTING_KIND,
            name=HOSTING_NAME,
            description=HOSTING_DESCRIPTION,
            amount_cents=HOSTING_AMOUNT_CENTS,
            interval=HOSTING_INTERVAL,
        )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            'Done. Add this line to your .env (and the prod server\'s '
            '.env) — then restart:\n'))
        self.stdout.write(
            f'    STRIPE_PRICE_HOSTING_YEARLY={hosting_price_id}\n')

    def _sync_one(self, *, kind, name, description, amount_cents,
                  interval):
        """Create-or-find the Product + a matching Price. Returns
        the Price ID."""
        product = self._find_product_by_kind(kind)
        if product is None:
            self.stdout.write(f'  creating Product: {name}')
            product = stripe.Product.create(
                name=name,
                description=description,
                metadata={'aspired_subscription_kind': kind},
            )
        else:
            self.stdout.write(
                f'  found existing Product: {product.id}  ({name})')

        price = self._find_active_price(
            product.id, amount_cents=amount_cents, interval=interval)
        if price is None:
            self.stdout.write(
                f'  creating Price: ${amount_cents/100:.2f} / {interval}')
            price = stripe.Price.create(
                product=product.id,
                unit_amount=amount_cents,
                currency='usd',
                recurring={'interval': interval},
                metadata={'aspired_subscription_kind': kind},
            )
        else:
            self.stdout.write(
                f'  found existing Price: {price.id}')

        return price.id

    def _find_product_by_kind(self, kind):
        products = stripe.Product.search(
            query=(f"active:'true' AND "
                   f"metadata['aspired_subscription_kind']:'{kind}'"),
            limit=10,
        )
        # Stripe Python v15 StripeObject has no .get(); use attribute access.
        data = list(getattr(products, 'data', None) or [])
        return data[0] if data else None

    def _find_active_price(self, product_id, *, amount_cents, interval):
        prices = stripe.Price.list(
            product=product_id, active=True, limit=20)
        for p in (getattr(prices, 'data', None) or []):
            rec = getattr(p, 'recurring', None)
            rec_interval = getattr(rec, 'interval', '') if rec else ''
            if (getattr(p, 'unit_amount', None) == amount_cents
                    and rec_interval == interval):
                return p
        return None
