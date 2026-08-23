"""
Delete leads. Destructive, so it argues with you first.

    python manage.py purge_leads --dry-run
    python manage.py purge_leads --source google_maps --dry-run
    python manage.py purge_leads --all --confirm

WHY A COMMAND AND NOT A SHELL ONE-LINER
---------------------------------------
A one-liner leaves no record of what was destroyed, cannot be reviewed
before it runs, and is copied from chat history months later by someone
who does not remember what it did. This prints exactly what will go,
requires --confirm to do anything, and says afterwards what survived.

WHAT SURVIVES, DELIBERATELY
---------------------------
``SuppressionList`` is never touched. Unsubscribes are permanent
(CLAUDE.md business rule 6), and the whole point of a separate table is
that deleting a lead cannot resurrect their consent. If a suppressed
address is scraped again tomorrow, ``import_leads`` will still refuse it.

``ApifyRun`` is never touched either -- it is the spend ledger, and
month-to-date cost is computed from it. Deleting rows there would make
the budget guard think money was available that has already been spent.

WHAT GOES WITH THE LEADS
------------------------
EmailSent, EmailReply, LeadNote and InstantlyEvent all cascade. That is
correct: an email row whose lead is gone is unreadable, and keeping the
send history of a deleted prospect helps nobody.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from outreach.models import (
    ApifyRun, EmailReply, EmailSent, InstantlyEvent, Lead, LeadNote,
    SuppressionList,
)


class Command(BaseCommand):
    help = 'Delete leads and their email history. Requires --confirm.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all', action='store_true',
            help='Every lead. Mutually exclusive with --source/--status.')
        parser.add_argument(
            '--source', default='',
            help='Only this source, e.g. google_maps, contact_form.')
        parser.add_argument(
            '--status', default='',
            help='Only this status, e.g. new, archived.')
        parser.add_argument(
            '--keep-suppressed', action='store_true', default=True,
            help='Kept for clarity; suppression is never deleted.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would go. Writes nothing.')
        parser.add_argument(
            '--confirm', action='store_true',
            help='Actually delete. Without this nothing is written.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok = self.style.SUCCESS
        bad = self.style.ERROR
        warn = self.style.WARNING
        head = self.style.MIGRATE_HEADING

        if not opts['all'] and not opts['source'] and not opts['status']:
            raise CommandError(
                'Refusing to guess. Pass --all, or narrow with --source '
                'and/or --status. Use --dry-run first.')

        leads = Lead.objects.all()
        if opts['source']:
            leads = leads.filter(source=opts['source'])
        if opts['status']:
            leads = leads.filter(status=opts['status'])

        total = leads.count()
        if not total:
            w(warn('Nothing matches. Nothing to do.'))
            return

        lead_ids = list(leads.values_list('pk', flat=True))
        sent = EmailSent.objects.filter(lead_id__in=lead_ids).count()
        replies = EmailReply.objects.filter(lead_id__in=lead_ids).count()
        notes = LeadNote.objects.filter(lead_id__in=lead_ids).count()
        events = InstantlyEvent.objects.filter(lead_id__in=lead_ids).count()

        w(head('\n=== WILL BE DELETED ==='))
        w(f'  leads          : {total}')
        for row in (leads.values('source')
                    .annotate(n=Count('id')).order_by('-n')):
            w(f"      {row['source']:16} {row['n']}")
        w(f'  EmailSent      : {sent}')
        w(f'  EmailReply     : {replies}')
        w(f'  LeadNote       : {notes}')
        w(f'  InstantlyEvent : {events}')

        w(head('\n=== WILL SURVIVE ==='))
        w(f'  SuppressionList : {SuppressionList.objects.count()} '
          f'(unsubscribes are permanent - a purged address that was '
          f'suppressed stays suppressed)')
        w(f'  ApifyRun        : {ApifyRun.objects.count()} '
          f'(spend ledger - the monthly budget guard reads it)')
        remaining = Lead.objects.exclude(pk__in=lead_ids).count()
        w(f'  other leads     : {remaining}')

        pushed = leads.exclude(instantly_lead_id='').count()
        if pushed:
            w(bad(
                f'\n  !! {pushed} of these were already pushed to an '
                f'Instantly campaign. Deleting them here does NOT remove '
                f'them from Instantly - they will keep receiving the '
                f'sequence. Remove them there first.'))

        if opts['dry_run'] or not opts['confirm']:
            w(warn('\nNothing deleted.'))
            w('Re-run with --confirm to go ahead.')
            return

        deleted, _ = leads.delete()
        w(ok(f'\nDeleted. {deleted} row(s) removed across all cascaded '
             f'tables.'))
        w(f'Leads remaining      : {Lead.objects.count()}')
        w(f'Suppression intact   : {SuppressionList.objects.count()}')
        w(f'Apify ledger intact  : {ApifyRun.objects.count()}')
