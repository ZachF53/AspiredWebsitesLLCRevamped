"""
seed_stripe_customers — ensure every existing client has a Stripe Customer.

Walks each ClientProfile that has no `stripe_customer_id` yet, creates a
Stripe Customer (via the idempotent billing.stripe_helpers.create_or_get
_customer), and stores the id. Saving the ClientProfile fires the
CP -> Account sync signal, so `Account.stripe_customer_id` (what the
portal actually reads) is populated too; we also set it explicitly as a
belt-and-suspenders guard against any sync gap.

Idempotent + dry-run by default. Run with --apply to write.
A client that already has a customer id is skipped (never re-created).

    python manage.py seed_stripe_customers           # dry-run, no writes
    python manage.py seed_stripe_customers --apply    # create for real

NOTE: this talks to whichever Stripe account STRIPE_SECRET_KEY points at
— TEST keys on staging, LIVE keys on prod. Run the dry-run first and read
the list before applying on prod.
"""

from django.core.management.base import BaseCommand

from billing.stripe_helpers import StripeNotConfigured, create_or_get_customer
from clients.models import ClientProfile


class Command(BaseCommand):
    help = 'Create a Stripe Customer for every client missing one.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write changes (default: dry-run, no writes).')

    def handle(self, *args, **options):
        apply = options['apply']
        mode = 'APPLY' if apply else 'DRY RUN - no writes'
        self.stdout.write(f'seed_stripe_customers ({mode})')

        profiles = ClientProfile.objects.select_related('user').order_by(
            'firm_name')
        total = profiles.count()
        created = skipped = failed = no_email = 0

        for cp in profiles:
            name = cp.firm_name or f'ClientProfile {cp.pk}'
            email = getattr(getattr(cp, 'user', None), 'email', '') or ''

            if cp.stripe_customer_id:
                skipped += 1
                self.stdout.write(
                    f'  SKIP  {name} - already has {cp.stripe_customer_id}')
                continue

            if not email:
                no_email += 1
                self.stdout.write(
                    f'  WARN  {name} - no email on file (customer will '
                    f'have no email)')

            if not apply:
                created += 1
                self.stdout.write(
                    f'  WOULD CREATE  {name} ({email or "no email"})')
                continue

            try:
                customer = create_or_get_customer(cp)
            except StripeNotConfigured as exc:
                self.stderr.write(self.style.ERROR(f'  Stripe error: {exc}'))
                return
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.stderr.write(self.style.ERROR(
                    f'  FAIL  {name} - {exc}'))
                continue

            # create_or_get_customer saved cp.stripe_customer_id, which
            # fires the CP->Account sync. Mirror it explicitly too in case
            # the account row was somehow out of sync.
            account = getattr(cp, 'migrated_account', None)
            if account is not None and not account.stripe_customer_id:
                account.stripe_customer_id = cp.stripe_customer_id
                account.save(update_fields=[
                    'stripe_customer_id', 'updated_at'])

            created += 1
            self.stdout.write(self.style.SUCCESS(
                f'  CREATED  {name} -> {customer.id}'))

        self.stdout.write('')
        verb = 'created' if apply else 'would create'
        self.stdout.write(
            f'Clients: {total} | {verb}: {created} | '
            f'skipped (already had one): {skipped} | '
            f'no email: {no_email} | failed: {failed}')
        if not apply and created:
            self.stdout.write(
                'Re-run with --apply to create these Stripe customers.')
