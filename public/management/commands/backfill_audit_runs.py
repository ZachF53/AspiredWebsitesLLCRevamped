"""
Backfill AuditRun from data we already hold.

Before AuditRun existed, an anonymous audit run left no row anywhere.
Two sources still remember some of them:

  * SessionRecording -- the tracker records the results page, and its
    title is "Audit Results for <url> | Aspired Websites". No scores,
    but the URL and the time. Recordings are kept ~30 days, so this
    reaches back only as far as the oldest one still on disk.
  * AuditLead -- every run where the visitor asked for the emailed
    report, with full scores.

The results page renders more than once per run (the email-capture POST
redirects back to it, and people refresh), so recordings of the same URL
within RELOAD_WINDOW collapse into one run.

Idempotent: a run already logged for the same URL within RELOAD_WINDOW
is never duplicated. Safe to re-run.

    python manage.py backfill_audit_runs [--days 60] [--dry-run]
"""

import re
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from public.models import AuditLead, AuditRun, audit_domain

RELOAD_WINDOW = timedelta(minutes=30)
# A lead is matched to a recorded run within this window either side.
LEAD_MATCH_WINDOW = timedelta(hours=2)

_TITLE_RE = re.compile(r'^Audit Results for (\S+)')
_RESULTS_URL_RE = r'^https://(www\.)?aspiredwebsites\.com/audit/results/'


class Command(BaseCommand):
    help = 'Backfill AuditRun rows from session recordings and AuditLeads.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=60)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, days, dry_run, **opts):
        from reporting.models import SessionRecording

        since = timezone.now() - timedelta(days=days)
        created = linked = 0

        with transaction.atomic():
            recordings = (SessionRecording.objects
                          .filter(created_at__gte=since,
                                  page_url__regex=_RESULTS_URL_RE)
                          .order_by('created_at')
                          .values_list('created_at', 'page_title'))
            for when, title in recordings:
                m = _TITLE_RE.match(title or '')
                if not m:
                    continue
                url = m.group(1)
                if self._existing(url, when, RELOAD_WINDOW):
                    continue
                self.stdout.write(f'  recording  {when:%Y-%m-%d %H:%M}  {url}')
                if not dry_run:
                    AuditRun.objects.create(
                        url=url, domain=audit_domain(url),
                        source='backfill_recording', created_at=when)
                created += 1

            for lead in AuditLead.objects.filter(created_at__gte=since):
                if lead.runs.exists():
                    continue
                run = self._existing(lead.url, lead.created_at,
                                     LEAD_MATCH_WINDOW, unlinked=True)
                if run:
                    self.stdout.write(
                        f'  link lead  {lead.created_at:%Y-%m-%d %H:%M}  '
                        f'{lead.url}')
                    if not dry_run:
                        run.audit_lead = lead
                        for f in ('performance_score', 'seo_score',
                                  'best_practices_score',
                                  'accessibility_score'):
                            if getattr(run, f) is None:
                                setattr(run, f, getattr(lead, f))
                        run.save()
                    linked += 1
                    continue
                self.stdout.write(
                    f'  lead       {lead.created_at:%Y-%m-%d %H:%M}  {lead.url}')
                if not dry_run:
                    AuditRun.objects.create(
                        url=lead.url, domain=audit_domain(lead.url),
                        performance_score=lead.performance_score,
                        seo_score=lead.seo_score,
                        best_practices_score=lead.best_practices_score,
                        accessibility_score=lead.accessibility_score,
                        audit_lead=lead, source='backfill_lead',
                        created_at=lead.created_at)
                created += 1

            if dry_run:
                transaction.set_rollback(True)

        prefix = '[dry run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}{created} run(s) created, {linked} linked to an '
            f'email request.'))

    @staticmethod
    def _existing(url, when, window, unlinked=False):
        """A run for the same site within `window` of `when`, if any.

        Matched on domain rather than the exact URL: the results-page
        title and the AuditLead can differ by scheme or trailing slash
        (http://x.com vs https://x.com/) for the same run.
        """
        qs = AuditRun.objects.filter(
            domain=audit_domain(url),
            created_at__gte=when - window,
            created_at__lte=when + window)
        if unlinked:
            qs = qs.filter(audit_lead__isnull=True)
        return qs.order_by('created_at').first()
