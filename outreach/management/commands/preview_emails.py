"""
Show the exact emails that would go out. Sends nothing.

    python manage.py preview_emails --campaign tx-law-firms --limit 5
    python manage.py preview_emails --limit 3 --all-touches

Instantly substitutes its own variables at send time on its side, which
means the text a prospect actually receives is never visible from Django
-- you approve a template and hope. This renders it locally against real
lead data so the thing being approved is the thing being sent.

Nothing here transmits. It reads the database, renders text, and prints.
"""

from django.core.management.base import BaseCommand, CommandError

from outreach import sequences, verify
from outreach.models import Lead, OutreachCampaign


class Command(BaseCommand):
    help = 'Render the real cold emails for sendable leads. Sends nothing.'

    def add_arguments(self, parser):
        parser.add_argument('--campaign', default='',
                            help='Campaign slug. Default: first active one.')
        parser.add_argument('--sequence', default='texas-law')
        parser.add_argument('--limit', type=int, default=5)
        parser.add_argument('--all-touches', action='store_true',
                            help='Show all 4 touches, not just the first.')
        parser.add_argument('--include-unsendable', action='store_true',
                            help='Also show leads the gates would reject.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok = self.style.SUCCESS
        bad = self.style.ERROR
        warn = self.style.WARNING
        head = self.style.MIGRATE_HEADING

        try:
            steps = sequences.build_steps(opts['sequence'])
        except sequences.SequenceError as exc:
            raise CommandError(str(exc))

        campaign = None
        if opts['campaign']:
            campaign = OutreachCampaign.objects.filter(
                slug=opts['campaign']).first()
            if campaign is None:
                raise CommandError(f"No campaign {opts['campaign']!r}")

        qs = Lead.objects.exclude(icebreaker='').exclude(email='')
        if not opts['include_unsendable']:
            qs = qs.filter(unsubscribed=False)
        leads = list(qs.order_by('-score')[:opts['limit']])

        if not leads:
            w(warn('No leads have an icebreaker yet. Run:'))
            w('  python manage.py shell -c "from outreach.tasks import '
              'generate_icebreakers_task; print(generate_icebreakers_task())"')
            return

        shown = 0
        for lead in leads:
            sendable = verify.is_sendable(lead.email_verification_status)
            if not sendable and not opts['include_unsendable']:
                continue
            shown += 1

            w(head(f'\n{"=" * 72}'))
            w(head(f'{lead.firm_name}'))
            w(f'  to        : {lead.attorney_name or "(no contact name)"} '
              f'<{lead.email}>')
            w(f'  location  : {lead.city}, {lead.state}')
            w(f'  website   : {lead.website or "(none)"}')
            w(f'  verified  : {lead.email_verification_status} '
              + (ok('SENDABLE') if sendable else bad('BLOCKED')))
            w(f'  measured  : ssl={lead.has_ssl} '
              f'site={lead.site_status or "live"} '
              f'perf={lead.website_performance_score} '
              f'copyright={lead.copyright_year}')
            w(f'  score     : {lead.score} ({lead.temperature})')
            w(head(f'{"-" * 72}'))

            to_show = steps if opts['all_touches'] else steps[:1]
            for i, step in enumerate(to_show, 1):
                rendered = sequences.render_for_lead(step, lead)
                leftover = (sequences.unresolved_variables(rendered['body'])
                            + sequences.unresolved_variables(
                                rendered['subject']))

                when = ('sent immediately' if i == 1
                        else f"day {sum(s['delay_days'] for s in steps[:i])}")
                if opts['all_touches']:
                    w(self.style.MIGRATE_LABEL(f'  [Touch {i} - {when}]'))
                w(f"  Subject: {rendered['subject'] or '(threads under previous)'}")
                w('')
                for line in rendered['body'].splitlines():
                    w(f'    {line}')
                w('')
                if leftover:
                    w(bad(f'  X UNRESOLVED VARIABLES: {leftover} '
                          f'- these would ship literally to the prospect.'))

        w(head(f'\n{"=" * 72}'))
        w(f'Showed {shown} lead(s). Nothing was sent.')

        blocked = Lead.objects.exclude(icebreaker='').exclude(
            email='').count() - shown
        if blocked > 0 and not opts['include_unsendable']:
            w(warn(f'{blocked} more lead(s) have copy but are blocked by a '
                   f'gate. See them with --include-unsendable.'))
        w('')
