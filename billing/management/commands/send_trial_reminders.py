"""
send_trial_reminders — email every free trial whose first charge is near.

    python manage.py send_trial_reminders
    python manage.py send_trial_reminders --dry-run

Celery beat runs this daily (billing.tasks.run_trial_reminder_sweep_task).
The command exists for two reasons: staging deliberately runs with
celerybeat stopped, so there the reminder has to be invokable by hand or
by cron, and --dry-run is the only safe way to see who is due before a
real send.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Send the pre-charge reminder email to free trials whose '
            'charge date is within TRIAL_REMINDER_DAYS.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List who is due without sending anything.',
        )

    def handle(self, *args, **options):
        try:
            import sys
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        from billing.trial_reminders import (
            due_reminders, reminder_days, run_trial_reminder_sweep,
        )

        if options['dry_run']:
            rows = list(due_reminders())
            if not rows:
                self.stdout.write(
                    f'No trials due a reminder (window: next '
                    f'{reminder_days()} days).')
                return
            self.stdout.write(
                f'{len(rows)} trial(s) due a reminder '
                f'(window: next {reminder_days()} days):')
            for row in rows:
                self.stdout.write(
                    f'  {row.email}  {row.product_slug}  '
                    f'charges {row.trial_end:%Y-%m-%d}')
            self.stdout.write('\nDry run: nothing sent.')
            return

        sent, failed = run_trial_reminder_sweep()
        self.stdout.write(f'Trial reminders: {sent} sent, {failed} failed.')
        if failed:
            self.stdout.write(
                'Failed rows keep reminder_sent_at empty and are retried '
                'on the next run.')
