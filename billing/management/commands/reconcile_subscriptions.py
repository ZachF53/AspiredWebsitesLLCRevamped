"""
Daily reconciliation cron — safety net for subscription state drift.

For every client with an active hosting subscription, confirm the
underlying resource is still alive:
  - Hosting sub: Droplet on our DO account
  - (future) Domain sub: domain still in our Namecheap account

If the resource is gone but the subscription is still active, cancel
the sub at period end + email an alert. This catches drift that the
real-time `invoice.upcoming` webhook would only catch ~3 days before
each renewal — useful when a Droplet is destroyed mid-cycle.

Wire to Celery beat in settings.CELERY_BEAT_SCHEDULE OR run from cron:
    python manage.py reconcile_subscriptions
"""

import logging

from django.core.management.base import BaseCommand


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Reconcile subscription state against underlying resources '
        '(Droplets, domains). Cancels drifted subscriptions and '
        'emails an alert.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be cancelled, but don\'t cancel.')

    def handle(self, *args, **options):
        from billing.webhooks import _droplet_alive
        from clients.models import ClientProfile

        dry = options['dry_run']
        prefix = '[DRY-RUN] ' if dry else ''

        # Active hosting subs.
        qs = ClientProfile.objects.exclude(
            stripe_hosting_subscription_id='')
        self.stdout.write(
            f'{prefix}Reconciling {qs.count()} hosting subscription(s) …')

        cancelled = 0
        ok = 0
        for client in qs:
            alive = _droplet_alive(client)
            if alive:
                ok += 1
                continue
            self.stdout.write(self.style.WARNING(
                f'  {prefix}DRIFT: {client.firm_name} — '
                f'droplet {client.do_droplet_id or "(unknown)"} not '
                f'active; sub {client.stripe_hosting_subscription_id} '
                f'should cancel'))
            if not dry:
                try:
                    from billing.stripe_helpers import (
                        cancel_hosting_subscription,
                    )
                    cancel_hosting_subscription(
                        client,
                        reason='reconcile_subscriptions: droplet missing',
                    )
                    cancelled += 1
                except Exception:
                    logger.exception(
                        'reconcile cancel failed for %s', client.pk)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Done. {ok} OK, {cancelled} cancelled, '
            f'{qs.count() - ok - cancelled} errored.'))
