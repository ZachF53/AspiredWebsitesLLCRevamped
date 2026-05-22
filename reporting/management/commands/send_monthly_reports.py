"""
send_monthly_reports — generate and email last month's PDF report to every
active maintenance client. Mirrors the 1st-of-month Celery beat task; safe to
run by hand for testing.
"""

from django.core.management.base import BaseCommand

from reporting.tasks import send_monthly_reports


class Command(BaseCommand):
    help = 'Generate and send monthly PDF reports to active maintenance clients.'

    def handle(self, *args, **options):
        result = send_monthly_reports()
        self.stdout.write(self.style.SUCCESS(result))
