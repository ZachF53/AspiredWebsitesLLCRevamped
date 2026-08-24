"""
The whole funnel, one screen, with the drop-off made visible.

Run it before and after anything, on any environment:

    python manage.py outreach_status

This exists because the failure that wasted 416 sends was invisible from
every view the system offered. The lead list showed 246 leads and the
outbox showed 416 emails, and both looked healthy. Nothing showed that
56% of those addresses were role mailboxes or scraped garbage, because
nothing measured it.

So every stage prints what entered, what left, and what was lost -- and
the losses are named rather than implied by a shrinking number.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count

from outreach import verify
from outreach.models import (
    EmailReply, EmailSent, InstantlyEvent, Lead, OutreachCampaign,
    SuppressionList,
)


class Command(BaseCommand):
    help = 'Show the cold outreach funnel, stage by stage, with drop-off.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--check-instantly', action='store_true',
            help='Also call the Instantly API for live mailbox capacity.')

    def handle(self, *args, **options):
        w = self.stdout.write
        bold = self.style.MIGRATE_HEADING
        ok = self.style.SUCCESS
        warn = self.style.WARNING
        bad = self.style.ERROR

        total = Lead.objects.count()

        w(bold('\n=== COLD OUTREACH FUNNEL ===\n'))

        # ── 1. Sourced ────────────────────────────────────────────────
        w(bold('1. SOURCED'))
        by_source = (Lead.objects.values('source')
                     .annotate(n=Count('id')).order_by('-n'))
        for row in by_source:
            w(f"     {row['source']:16} {row['n']:>6}")
        w(f"     {'TOTAL':16} {total:>6}")

        inbound = Lead.objects.filter(
            source__in=Lead.INBOUND_SOURCES).count()
        if inbound:
            w(f'\n     {inbound} of these are INBOUND (they contacted us).')
            w('     They are held out of cold outreach entirely and get a')
            w('     human reply instead. Not a drop-off - a decision.')

        with_email = Lead.objects.exclude(email='').count()
        w(f"\n     with an email address: {with_email} / {total}")
        if total and with_email / total < 0.8:
            w(warn('     !  A source that does not return emails is a '
                   'source of leads you cannot contact.'))

        # ── 2. Verified ───────────────────────────────────────────────
        w(bold('\n2. VERIFIED  (outreach/verify.py)'))
        counts = dict(
            Lead.objects.values_list('email_verification_status')
            .annotate(n=Count('id')).order_by())
        order = [verify.VALID, verify.CONSUMER, verify.UNVERIFIED,
                 verify.RISKY, verify.ROLE, verify.INVALID, verify.PENDING]
        for status in order:
            n = counts.get(status, 0)
            if not n:
                continue
            sendable = verify.is_sendable(status)
            mark = ok('  send') if sendable else bad(' BLOCK')
            w(f"     {status:12} {n:>6} {mark}   "
              f"{verify.rejection_reason(status)[:52]}")

        pending = counts.get(verify.PENDING, 0)
        if pending:
            w(warn(f'\n     {pending} lead(s) never verified -- run '
                   f'verify_leads_task or `manage.py shell`:'))
            w('       from outreach.tasks import verify_leads_task; '
              'verify_leads_task()')

        sendable_total = sum(
            n for status, n in counts.items() if verify.is_sendable(status))
        w(f"\n     sendable: {sendable_total} / {total}")

        # ── 3. Enriched ───────────────────────────────────────────────
        w(bold('\n3. ENRICHED  (outreach/enricher.py)'))
        enriched = Lead.objects.filter(
            enrichment_completed_at__isnull=False).count()
        w(f'     enrichment complete : {enriched}')
        w(f'     has PageSpeed score : '
          f'{Lead.objects.filter(website_performance_score__isnull=False).count()}')
        w(f'     no SSL (http only)  : '
          f'{Lead.objects.filter(has_ssl=False).count()}   '
          f'<- strongest opening line for law/medical')
        w(f'     stale copyright     : '
          f'{Lead.objects.filter(copyright_year__lt=2023).exclude(copyright_year=None).count()}')

        # ── 4. Personalised ───────────────────────────────────────────
        w(bold('\n4. PERSONALISED  (outreach/icebreaker.py)'))
        has_ice = Lead.objects.exclude(icebreaker='').count()
        w(f'     has an icebreaker   : {has_ice}')
        ready = Lead.objects.exclude(icebreaker='').exclude(
            email='').filter(unsubscribed=False, instantly_lead_id='').count()
        w(f'     ready to push       : {ready}')

        # ── 4b. Manual review ─────────────────────────────────────────
        w(bold('\n4b. AWAITING REVIEW  (outreach/review.py)'))
        flagged = Lead.objects.filter(needs_review=True)
        w(f'     flagged             : {flagged.count()}')
        if flagged.exists():
            w('     These are HELD, not dropped. Clear them at')
            w('     /admin-dashboard/outreach/review/')
            for lead in flagged[:5]:
                w(f'       {lead.firm_name[:32]:34} '
                  f'{lead.review_reason[:46]}')

        # ── 5. Campaigns ──────────────────────────────────────────────
        w(bold('\n5. SEGMENTED  (OutreachCampaign)'))
        campaigns = OutreachCampaign.objects.all()
        if not campaigns:
            w(warn('     No campaigns defined. Nothing can be pushed.'))
            w('     Create one: /admin-dashboard/outreach/campaigns/new/')
        for c in campaigns:
            state = ok('active') if c.is_pushable else warn('not pushable')
            linked = c.instantly_campaign_id or bad('NO INSTANTLY ID')
            target = f'/{c.lead_target}' if c.lead_target else ''
            full = warn('  FULL') if c.is_full else ''
            w(f'     {c.name:28} {state}  '
              f'leads={c.leads.count():>5}{target}  '
              f'pushed={c.leads_pushed:>5}  {linked}{full}')
            if c.last_push_error:
                w(bad(f'        last error: {c.last_push_error[:70]}'))

        # ── 5b. Assigned ──────────────────────────────────────────────
        #
        # This stage is printed separately because its failure is silent.
        # A ready lead with no campaign is not an error and not in a
        # campaign, so before this block existed nothing anywhere showed
        # it -- the pipeline logged "nothing ready" and looked healthy.
        w(bold('\n5b. ASSIGNED  (outreach/assignment.py)'))
        from outreach.assignment import assignable_leads, open_campaigns
        waiting = assignable_leads().count()
        arms = open_campaigns()
        w(f'     arms accepting leads : {len(arms)}')
        w(f'     ready but unassigned : {waiting}')
        if waiting and not arms:
            w(bad('     !  These leads are finished and have nowhere to '
                  'go. Every arm is'))
            w(bad('        inactive, missing an Instantly id, or full.'))
        elif waiting:
            w('     Next pipeline run will place them. To preview the '
              'split first:')
            w('       python manage.py assign_campaigns --dry-run')

        # ── 6. Sending ────────────────────────────────────────────────
        w(bold('\n6. SENDING  (Instantly)'))
        pushed = Lead.objects.exclude(instantly_lead_id='').count()
        w(f'     pushed to Instantly : {pushed}')
        if options['check_instantly']:
            from outreach import instantly
            status = instantly.connection_status()
            if not status['connected']:
                w(bad(f"     API: {status['reason'][:80]}"))
            else:
                w(ok(f"     API: connected"))
                w(f"     mailboxes  : {status['mailboxes_active']}"
                  f" / {status['mailboxes_total']} active")
                w(f"     capacity   : {status['daily_capacity']}/day")
                w(f"     domains    : {', '.join(status['domains'])}")
        else:
            w('     (pass --check-instantly for live mailbox capacity)')

        # ── 7. Replies ────────────────────────────────────────────────
        w(bold('\n7. REPLIES'))
        legacy_sent = EmailSent.objects.filter(status='sent').count()
        replies = EmailReply.objects.count()
        w(f'     legacy SendGrid sends : {legacy_sent}')
        w(f'     EmailReply rows       : {replies}')
        by_class = (EmailReply.objects.values('classification')
                    .annotate(n=Count('id')).order_by('-n'))
        for row in by_class:
            w(f"       {row['classification'] or '(unclassified)':22} "
              f"{row['n']:>4}")

        events = InstantlyEvent.objects.count()
        w(f'     Instantly events      : {events}')
        if events:
            by_event = (InstantlyEvent.objects.values('event_type')
                        .annotate(n=Count('id')).order_by('-n'))
            for row in by_event:
                w(f"       {row['event_type']:22} {row['n']:>4}")
            dropped = InstantlyEvent.objects.exclude(error='').count()
            if dropped:
                w(f'       dropped by the filter: {dropped}  '
                  f'(this is the filter working, not a bug)')

        # ── 8. Suppression ────────────────────────────────────────────
        w(bold('\n8. SUPPRESSED (permanent)'))
        w(f'     suppression list : {SuppressionList.objects.count()}')
        w(f'     unsubscribed     : '
          f'{Lead.objects.filter(unsubscribed=True).count()}')

        # ── Bottom line ───────────────────────────────────────────────
        w(bold('\n=== WHERE THE FUNNEL STOPS ==='))
        blockers = []
        if not Lead.objects.exists():
            blockers.append('No leads. Source some first.')
        if pending:
            blockers.append(f'{pending} leads unverified.')
        if sendable_total and not has_ice:
            blockers.append(
                f'{sendable_total} sendable leads have no icebreaker.')
        if not campaigns.filter(active=True).exists():
            blockers.append('No active campaign.')
        elif not campaigns.exclude(instantly_campaign_id='').exists():
            blockers.append(
                'No campaign has an instantly_campaign_id -- create the '
                'campaign in Instantly and paste its id in.')
        elif waiting and not arms:
            blockers.append(
                f'{waiting} leads are ready but every arm is full or '
                f'closed -- raise lead_target or open another arm.')

        if blockers:
            for b in blockers:
                w(bad(f'  X {b}'))
        else:
            w(ok('  OK Nothing blocking. Leads can flow to Instantly.'))
        w('')
