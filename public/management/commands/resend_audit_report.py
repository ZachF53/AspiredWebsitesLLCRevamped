"""
Resend the detailed audit report for one AuditLead.

    python manage.py resend_audit_report <lead_id> [--refresh] [--to addr]

--refresh re-runs PageSpeed on the lead's URL first and stores the fresh
scores and full check lists. Leads captured before full details were
saved need it to get the "what passed" lists. A missing Aspired AI review
is generated when ANTHROPIC_API_KEY is set.

--to sends a preview to another address without touching the lead's
report_sent_at.
"""

import copy

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from public import audit_sequence
from public.models import AuditLead


class Command(BaseCommand):
    help = 'Resend the detailed audit report email for one AuditLead.'

    def add_arguments(self, parser):
        parser.add_argument('lead_id', type=int)
        parser.add_argument('--refresh', action='store_true')
        parser.add_argument('--to', default='')

    def handle(self, *args, lead_id, refresh, to, **opts):
        from public.views import (
            _flatten_issues, _generate_ai_audit_review, _PageSpeedError,
            _run_pagespeed_audit,
        )

        lead = AuditLead.objects.filter(pk=lead_id).first()
        if not lead:
            raise CommandError(f'No AuditLead {lead_id}')

        if refresh or not lead.details:
            self.stdout.write(f'Re-running PageSpeed on {lead.url} ...')
            try:
                result = _run_pagespeed_audit(lead.url)
            except _PageSpeedError as err:
                raise CommandError(str(err))
            s = result['scores']
            lead.performance_score = s['performance']
            lead.seo_score = s['seo']
            lead.best_practices_score = s['best_practices']
            lead.accessibility_score = s['accessibility']
            lead.issues = result['issues_by_category']
            lead.details = result.get('details') or {}
            lead.ai_review = ''
            lead.save()

        if not lead.ai_review and settings.ANTHROPIC_API_KEY:
            scores = {
                'performance': lead.performance_score,
                'seo': lead.seo_score,
                'best_practices': lead.best_practices_score,
                'accessibility': lead.accessibility_score,
            }
            try:
                lead.ai_review = _generate_ai_audit_review(
                    lead.url, scores, _flatten_issues(lead.issues))
                lead.save(update_fields=['ai_review'])
            except Exception as err:  # noqa: BLE001
                self.stderr.write(f'AI review skipped: {err}')

        if to:
            preview = copy.copy(lead)
            preview.email = to
            subject, body = audit_sequence.build_report(preview)
            audit_sequence._send_report_html(preview, subject, body)
            self.stdout.write(self.style.SUCCESS(f'Preview sent to {to}'))
            return

        if audit_sequence.send_report(lead):
            self.stdout.write(self.style.SUCCESS(
                f'Report sent to {lead.email}'))
        else:
            self.stdout.write(self.style.WARNING(
                'Not sent (unsubscribed, suppressed or no email).'))
