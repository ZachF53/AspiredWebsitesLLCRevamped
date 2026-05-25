"""
Backfill ``do_droplet_id`` + ``do_droplet_name`` on Website (and on the
legacy ClientProfile mirror) by matching DigitalOcean droplets by IP.

Why this exists: when these clients were originally provisioned, only
the droplet IP got captured — the droplet ID + name were never
written back. The Website admin page now shows DO control-panel links
that need the ID, so we resolve it from DO's API once.

Idempotent: re-running matches and writes the same values; skips rows
that already have the ID set.

Usage:
  python manage.py backfill_droplet_ids --dry-run
  python manage.py backfill_droplet_ids
"""

import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Match Website.do_droplet_ip against DigitalOcean droplets and '
        'fill in the missing do_droplet_id + do_droplet_name. Mirrors '
        'the writes to the legacy ClientProfile so both sides see the '
        'same data through the Phase D drop.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change; write nothing.')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']

        from billing.do_helpers import get_all_droplets
        from clients.account_models import Website

        self.stdout.write('Pulling all droplets from DigitalOcean…')
        droplets = get_all_droplets()
        if not droplets:
            self.stderr.write(self.style.ERROR(
                'No droplets returned (API token unset, or DO is down). '
                'Aborting — nothing to backfill against.'))
            return

        # Index droplets by IP for O(1) lookups.
        by_ip = {}
        for d in droplets:
            if d['ip']:
                by_ip[d['ip']] = d
        self.stdout.write(
            f'  → {len(droplets)} droplets fetched, '
            f'{len(by_ip)} have public IPs.')
        self.stdout.write('')

        targets = Website.objects.filter(
            do_droplet_ip__isnull=False).exclude(do_droplet_ip='')

        matched = 0
        already_set = 0
        no_match = 0
        for w in targets:
            ip = str(w.do_droplet_ip)
            droplet = by_ip.get(ip)
            if droplet is None:
                self.stdout.write(self.style.WARNING(
                    f'  ✗ {w.name} ({ip}) — no DO droplet found at this IP'))
                no_match += 1
                continue

            droplet_id = str(droplet['id'])
            droplet_name = droplet['name']

            # Already populated? Skip silently — idempotent re-runs.
            if (w.do_droplet_id == droplet_id
                    and w.do_droplet_name == droplet_name):
                already_set += 1
                continue

            self.stdout.write(self.style.SUCCESS(
                f'  ✓ {w.name} ({ip}) → id={droplet_id} name={droplet_name}'
                + (' [DRY RUN]' if dry_run else '')))

            if not dry_run:
                w.do_droplet_id = droplet_id
                w.do_droplet_name = droplet_name
                w.save(update_fields=[
                    'do_droplet_id', 'do_droplet_name', 'updated_at'])

                # Mirror to the legacy ClientProfile so the old admin
                # view + any code still reading from CP sees it too.
                # The legacy model has do_droplet_id but no name, so
                # only the id propagates.
                cp = w.account.legacy_client_profile
                if cp and cp.do_droplet_id != droplet_id:
                    cp.do_droplet_id = droplet_id
                    cp.save(update_fields=['do_droplet_id', 'updated_at'])
            matched += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. matched: {matched}  already-set: {already_set}  '
            f'no-match: {no_match}'))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'No writes performed. Re-run without --dry-run to apply.'))
