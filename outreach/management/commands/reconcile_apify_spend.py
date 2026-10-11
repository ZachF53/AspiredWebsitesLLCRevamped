"""
Pull real Apify runs into the local ledger so the budget guard is honest.

    python manage.py reconcile_apify_spend            # show the gap
    python manage.py reconcile_apify_spend --write    # close it

WHY THIS IS NEEDED
------------------
``apify_source._month_to_date_cost()`` reads the ``ApifyRun`` table, which
only ever gets a row when Django starts the run. Any run triggered in the
Apify Console is therefore invisible to the guard, and the guard is the
thing standing between a typo and the monthly plan.

Measured on 2026-10-10: the ledger's newest row was 2026-08-29, while the
account had actually spent $0.22 on 2026-09-14 and $5.02 on 2026-10-01
from console-triggered runs. The guard reported $0.00 spent for October
against a $14 cap. It would have approved roughly $14 of further spend on
top of $5 already gone, on a $29 plan.

The console path is not a mistake to be stamped out either — it is the
documented workaround for actors that refuse API runs, so console runs
will keep happening and the ledger has to be reconciled rather than
assumed complete.

Matching is on ``apify_run_id``, so this is idempotent: re-running it
never double-counts a run it already recorded.
"""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from outreach.apify_source import budget_status
from outreach.models import ApifyRun


class Command(BaseCommand):
    help = ('Reconcile the ApifyRun ledger against the Apify API so '
            'console-triggered runs count against the budget guard.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--write', action='store_true',
            help='Insert the missing rows. Without this, report only.')
        parser.add_argument(
            '--limit', type=int, default=100,
            help='How many recent Apify runs to examine. Default 100.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok = self.style.SUCCESS
        warn = self.style.WARNING
        bold = self.style.MIGRATE_HEADING

        token = getattr(settings, 'APIFY_TOKEN', '')
        if not token:
            raise CommandError('APIFY_TOKEN is not set.')

        try:
            resp = requests.get(
                'https://api.apify.com/v2/actor-runs',
                headers={'Authorization': f'Bearer {token}'},
                params={'limit': opts['limit'], 'desc': 'true'},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise CommandError(f'Apify API unreachable: {exc}')
        if resp.status_code >= 400:
            raise CommandError(
                f'Apify HTTP {resp.status_code}: {resp.text[:200]}')

        runs = ((resp.json() or {}).get('data') or {}).get('items') or []
        known = {
            r.apify_run_id: r for r in
            ApifyRun.objects.exclude(apify_run_id='')}

        missing = [r for r in runs if r.get('id') not in known]

        # Runs we DO have a row for, whose recorded cost is stale. A run's
        # usageTotalUsd keeps settling after it reports SUCCEEDED, so the
        # figure apify_maps/apify_source wrote at finish is a floor rather
        # than the final bill. Inserting missing runs but never truing up
        # the ones we already had would leave the guard permanently
        # under-reporting the runs Django itself started.
        stale = []
        for r in runs:
            row = known.get(r.get('id'))
            if row is None:
                continue
            cost = r.get('usageTotalUsd')
            if not isinstance(cost, (int, float)):
                continue
            real = Decimal(str(round(float(cost), 4)))
            if row.actual_cost_usd is None or row.actual_cost_usd != real:
                stale.append((row, real))

        w(bold('\n=== APIFY SPEND RECONCILE ==='))
        w(f'  runs on Apify (last {opts["limit"]}) : {len(runs)}')
        w(f'  already in the ledger            : {len(runs) - len(missing)}')
        w(f'  MISSING from the ledger          : {len(missing)}')
        w(f'  known but cost is stale          : {len(stale)}')

        before = budget_status()
        w(f'\n  ledger says spent this month    : '
          f'${before["spent_usd"]:.2f} / ${before["budget_usd"]:.2f}')

        if stale:
            w(bold('\n  Stale costs:'))
            for row, real in stale:
                w(f'    {row.apify_run_id}  '
                  f'recorded ${row.actual_cost_usd} -> actual ${real}')

        if not missing and not stale:
            w(ok('\n  Ledger is complete and costs agree. Nothing to do.\n'))
            return

        if missing:
            w(bold('\n  Missing runs:'))
        unrecorded = Decimal('0')
        for r in missing:
            cost = r.get('usageTotalUsd')
            cost_dec = (Decimal(str(round(float(cost), 4)))
                        if isinstance(cost, (int, float)) else Decimal('0'))
            unrecorded += cost_dec
            # origin distinguishes a console run from one Django started.
            # WEB means a human was in the Apify UI, which is the whole
            # reason this command has to exist.
            origin = (r.get('meta') or {}).get('origin') or '?'
            w(f"    {str(r.get('startedAt'))[:19]}  "
              f"{str(r.get('status')):10} origin={origin:4} "
              f"actor={r.get('actId')}  ${cost_dec}")

        w(f'\n  unrecorded spend in this window : ${unrecorded}')

        if not opts['write']:
            w(warn('\n  REPORT ONLY - pass --write to apply these '
                   'changes.\n'))
            return

        trued = 0
        for row, real in stale:
            row.actual_cost_usd = real
            row.save(update_fields=['actual_cost_usd'])
            trued += 1

        created = 0
        for r in missing:
            cost = r.get('usageTotalUsd')
            cost_dec = (Decimal(str(round(float(cost), 4)))
                        if isinstance(cost, (int, float)) else None)
            started = _parse_dt(r.get('startedAt'))
            finished = _parse_dt(r.get('finishedAt'))
            # Apify's status vocabulary is not ours; anything that is not
            # a clean SUCCEEDED is recorded as failed so it still carries
            # its cost without reading as a good run.
            status = ('succeeded' if r.get('status') == 'SUCCEEDED'
                      else 'failed')
            row = ApifyRun.objects.create(
                actor_id=str(r.get('actId') or ''),
                apify_run_id=str(r.get('id') or ''),
                dataset_id=str(r.get('defaultDatasetId') or ''),
                status=status,
                label=f"[reconciled] {r.get('actId')} "
                      f"{str(r.get('startedAt'))[:10]}",
                estimated_cost_usd=cost_dec or Decimal('0'),
                actual_cost_usd=cost_dec,
                finished_at=finished,
                error=('Reconciled from the Apify API - this run was not '
                       'started by Django, so no local ledger row existed.'),
            )
            # started_at is auto_now_add, so it has to be corrected after
            # insert or every reconciled run lands in the current month
            # and the month-to-date figure is wrong in the other
            # direction.
            if started:
                ApifyRun.objects.filter(pk=row.pk).update(started_at=started)
            created += 1

        after = budget_status()
        w(ok(f'\n  inserted {created} ledger row(s), '
             f'trued up {trued} stale cost(s)'))
        w(f'  ledger now says spent this month: '
          f'${after["spent_usd"]:.2f} / ${after["budget_usd"]:.2f}')
        w(f'  remaining                       : '
          f'${after["remaining_usd"]:.2f}\n')


def _parse_dt(value):
    """Apify ISO-8601 with a trailing Z -> aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace('Z', '+00:00')).astimezone(dt_timezone.utc)
    except (TypeError, ValueError):
        return None
