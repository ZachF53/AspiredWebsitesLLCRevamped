"""
Record that an operator confirmed a payment the ledger cannot show.

Some payments never touch Stripe — a cheque, a bank transfer, Zelle. The
build is genuinely paid, but there is no PaymentRecord, no invoice and no
timestamp, so `payment_status = 'fully_paid'` reads as an unsupported
claim: the parity audit reports it and the launch gate blocks on it, every
time, forever.

This command settles it the honest way. It writes down that a named person
confirmed the payment and when — it does NOT write a PaymentRecord, an
invoice, a contract or a paid-at timestamp. Fabricating a transaction to
silence a warning would corrupt the billing ledger and every revenue,
tax and reconciliation figure drawn from it. An attestation is auditable;
an invented receipt is a lie that compounds.

Once recorded:
  * `audit_account_website_parity` stops reporting the site,
  * the launch gate in `clients.services` lets it through,
  * and the reason stays attached to the row.

    python manage.py verify_website_payment whitehead-wellness \\
        --by "Zachery Long" --note "Paid in full outside Stripe" --apply
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = ('Record an operator confirmation that a website was paid '
            'outside the billing ledger. Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument(
            'slug', type=str,
            help='Website slug, e.g. whitehead-wellness.')
        parser.add_argument(
            '--by', type=str, default='',
            help='Who is confirming the payment. Required to apply.')
        parser.add_argument(
            '--note', type=str, default='',
            help='How it was paid and how you confirmed it.')
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the attestation (default: report only).')
        parser.add_argument(
            '--clear', action='store_true',
            help='Remove a previously recorded attestation.')

    def handle(self, *args, **opts):
        from clients.account_models import Website
        from clients.payment_evidence import ledger_evidence_for

        website = Website.objects.filter(slug=opts['slug']).first()
        if website is None:
            raise CommandError(f'No website with slug {opts["slug"]!r}.')

        self.stdout.write(f'{website.name} ({website.slug})')
        self.stdout.write(f'  payment_status: {website.payment_status}')
        existing = ledger_evidence_for(website)
        self.stdout.write(
            f'  ledger evidence: {"; ".join(existing) if existing else "none"}')

        if opts['clear']:
            if not opts['apply']:
                self.stdout.write(
                    '\nWould clear the attestation. Re-run with --apply.')
                return
            website.payment_verified_at = None
            website.payment_verified_by = ''
            website.payment_verification_note = ''
            website.save(update_fields=[
                'payment_verified_at', 'payment_verified_by',
                'payment_verification_note', 'updated_at'])
            self.stdout.write(self.style.SUCCESS('\nAttestation cleared.'))
            return

        if website.payment_verified_at:
            self.stdout.write(self.style.SUCCESS(
                '\nAlready verified — nothing to do.'))
            return

        if website.payment_status != 'fully_paid':
            self.stdout.write(self.style.WARNING(
                f'\nNote: payment_status is {website.payment_status!r}, not '
                "'fully_paid'. This records that payment was confirmed; it "
                'does not change the payment status.'))

        if not opts['apply']:
            self.stdout.write(
                '\nDRY RUN — would record an operator confirmation.')
            self.stdout.write('Re-run with --by "Your Name" --apply.')
            return

        if not opts['by'].strip():
            raise CommandError(
                '--by is required to apply: an attestation with nobody '
                'attached is not evidence.')

        website.payment_verified_at = timezone.now()
        website.payment_verified_by = opts['by'].strip()
        website.payment_verification_note = opts['note'].strip()
        website.save(update_fields=[
            'payment_verified_at', 'payment_verified_by',
            'payment_verification_note', 'updated_at'])

        self.stdout.write(self.style.SUCCESS(
            f'\nRecorded: confirmed by {website.payment_verified_by} on '
            f'{website.payment_verified_at:%Y-%m-%d}.'))
        self.stdout.write(
            'No PaymentRecord, invoice or timestamp was fabricated. The '
            'parity audit and launch gate will now accept this site.')
