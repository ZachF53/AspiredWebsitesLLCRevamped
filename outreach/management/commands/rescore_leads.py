"""
rescore_leads — recompute Lead.score for every existing row using
the current enrichment-aware scoring logic.

Use after deploying a scoring change, or to clean up legacy rows
whose stored score was set at import time (before the enricher
filled in PageSpeed / SSL / socials).

Usage:
    python manage.py rescore_leads               # rescore all
    python manage.py rescore_leads --enriched    # only enriched leads
    python manage.py rescore_leads --dry-run     # report deltas, write nothing
"""

from django.core.management.base import BaseCommand

from outreach.enricher import _rescore_from_model
from outreach.models import Lead


class Command(BaseCommand):
    help = 'Recompute Lead.score for every existing lead.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--enriched', action='store_true',
            help='Only rescore leads with enrichment_completed_at set.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change, write nothing.')

    def handle(self, *args, **opts):
        # Windows CP1252 stdout chokes on the → arrow used below;
        # reconfigure to UTF-8 with replacement so a missing glyph
        # never crashes the run.
        import sys
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        qs = Lead.objects.all()
        if opts['enriched']:
            qs = qs.filter(enrichment_completed_at__isnull=False)

        total = qs.count()
        self.stdout.write(f'Scanning {total} leads…')

        unchanged = changed = 0
        delta_up = delta_down = 0
        for lead in qs.iterator(chunk_size=200):
            new_score, new_temp = _rescore_from_model(lead)
            if new_score == lead.score and new_temp == lead.temperature:
                unchanged += 1
                continue
            changed += 1
            if new_score > lead.score:
                delta_up += 1
            elif new_score < lead.score:
                delta_down += 1
            self.stdout.write(
                f'  {lead.firm_name[:50]:<50}  '
                f'{lead.score}({lead.temperature}) → '
                f'{new_score}({new_temp})')
            if not opts['dry_run']:
                lead.score = new_score
                lead.temperature = new_temp
                lead.save(update_fields=['score', 'temperature', 'updated_at'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. {changed} changed ({delta_up} up, {delta_down} down), '
            f'{unchanged} unchanged.'
            + (' (dry-run — no writes)' if opts['dry_run'] else '')
        ))
