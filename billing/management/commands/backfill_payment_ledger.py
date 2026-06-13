"""
Backfill the PaymentRecord ledger from Stripe — the source of truth for
actual charges. Captures EVERY payment:

  - one-time card charges  → succeeded PaymentIntents NOT tied to an invoice
                             (website deposit / final / pay-in-full)
  - recurring charges      → paid Invoices (maintenance / social / hosting)

Idempotent (keyed on the Stripe id). Going forward the webhooks record
these live; this command seeds history (e.g. payments made before the
ledger existed) and can be re-run safely.

Usage:
    python manage.py backfill_payment_ledger --all
    python manage.py backfill_payment_ledger --customer cus_XXX
    python manage.py backfill_payment_ledger --all --rebuild   # wipe + redo
"""

from datetime import datetime, timezone as _tz

from django.core.management.base import BaseCommand


def _ts(v):
    return datetime.fromtimestamp(int(v), tz=_tz.utc) if v else None


def _meta_get(obj, key):
    """Read a metadata key whether Stripe gave us a dict or a StripeObject."""
    md = getattr(obj, 'metadata', None) or {}
    if isinstance(md, dict):
        return md.get(key)
    return getattr(md, key, None)


def _kind_from_pi_desc(desc):
    d = (desc or '').lower()
    if 'deposit' in d:
        return 'deposit'
    if 'final' in d:
        return 'final'
    return 'build'


def _kind_for_sub(sub_id, cp):
    from clients.service_models import MaintenancePlan, SocialMediaPlan
    if sub_id and sub_id == cp.stripe_hosting_subscription_id:
        return 'hosting', None
    mp = MaintenancePlan.objects.filter(stripe_subscription_id=sub_id).first()
    if mp or sub_id == cp.stripe_subscription_id:
        return 'maintenance', (mp.website if mp else None)
    sp = SocialMediaPlan.objects.filter(stripe_subscription_id=sub_id).first()
    if sp or sub_id == cp.stripe_social_subscription_id:
        return 'social', (sp.website if sp else None)
    return 'other', None


class Command(BaseCommand):
    help = 'Backfill the PaymentRecord ledger from Stripe charges.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true')
        parser.add_argument('--customer', default='')
        parser.add_argument('--rebuild', action='store_true',
                            help='Delete existing records first, then rebuild.')

    def handle(self, *args, **opts):
        import stripe

        from billing.stripe_helpers import _init
        from billing.webhooks import _record_payment
        from clients.models import ClientProfile, PaymentRecord
        _init()

        qs = ClientProfile.objects.exclude(stripe_customer_id='')
        if opts['customer']:
            qs = qs.filter(stripe_customer_id=opts['customer'])
        elif not opts['all']:
            self.stderr.write('Pass --all or --customer cus_XXX')
            return

        before = PaymentRecord.objects.count()
        for cp in qs:
            cust = cp.stripe_customer_id
            if opts['rebuild']:
                PaymentRecord.objects.filter(client=cp).delete()

            # One-time card charges (not tied to an invoice).
            try:
                for pi in stripe.PaymentIntent.list(
                        customer=cust, limit=100).auto_paging_iter():
                    if getattr(pi, 'status', '') != 'succeeded':
                        continue
                    # Only OUR one-time build charges (deposit/final/full).
                    # Subscription PIs are captured via their Invoice below.
                    if _meta_get(pi, 'kind') != 'onboarding':
                        continue
                    amt = (getattr(pi, 'amount_received', 0)
                           or getattr(pi, 'amount', 0) or 0) / 100
                    if amt <= 0:
                        continue
                    desc = (getattr(pi, 'description', '') or 'Website payment')
                    desc = desc.replace('Aspired Websites — ', '')
                    _record_payment(
                        client=cp, stripe_id=pi.id,
                        kind=_kind_from_pi_desc(desc), amount=amt,
                        description=desc, paid_at=_ts(getattr(pi, 'created', None)))
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'PI list failed for {cust}: {exc}')

            # Recurring / invoice charges.
            try:
                for inv in stripe.Invoice.list(
                        customer=cust, limit=100).auto_paging_iter():
                    if getattr(inv, 'status', '') != 'paid':
                        continue
                    amt = (getattr(inv, 'amount_paid', 0) or 0) / 100
                    if amt <= 0:
                        continue
                    sub_id = getattr(inv, 'subscription', '') or ''
                    if sub_id:
                        kind, web = _kind_for_sub(sub_id, cp)
                        # Fall back to the subscription's product_type metadata.
                        if kind == 'other':
                            try:
                                sub = stripe.Subscription.retrieve(sub_id)
                                pt = _meta_get(sub, 'product_type') or ''
                                kind = {'maintenance': 'maintenance',
                                        'social_media': 'social',
                                        'hosting': 'hosting'}.get(pt, 'other')
                            except Exception:
                                pass
                    else:
                        kind, web = 'other', None
                    ld = ''
                    try:
                        ld = inv.lines.data[0].description
                    except Exception:
                        ld = ''
                    # Last-resort label from the line description (legacy
                    # subs with no plan row / metadata).
                    if kind == 'other':
                        dl = (ld or '').lower()
                        if any(t in dl for t in (
                                'essentials', 'growth', 'dominant',
                                'maintenance')):
                            kind = 'maintenance'
                        elif any(t in dl for t in (
                                'basic', 'standard', 'full management',
                                'social')):
                            kind = 'social'
                        elif 'hosting' in dl:
                            kind = 'hosting'
                    _record_payment(
                        client=cp, stripe_id=inv.id, kind=kind, amount=amt,
                        description=ld or f'{kind.title()} charge',
                        paid_at=_ts(getattr(inv, 'created', None)),
                        website=web,
                        receipt_url=getattr(inv, 'hosted_invoice_url', '') or '')
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'Invoice list failed for {cust}: {exc}')

        after = PaymentRecord.objects.count()
        self.stdout.write(
            f'Ledger records: {before} -> {after} '
            f'(+{after - before}) across {qs.count()} client(s).')
