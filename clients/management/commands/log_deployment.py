"""
log_deployment — record a deployment in a client's site changelog.

Usage:
    python manage.py log_deployment <client_id> \
        --title "Deployed latest updates" \
        --description "Code update, migrations, static files refreshed"

Intended as an optional last step of deploy.sh — see the commented block at
the end of deploy.sh.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from clients.models import ClientProfile, SiteChangelogEntry


class Command(BaseCommand):
    help = "Record a deployment in a client's site changelog."

    def add_arguments(self, parser):
        parser.add_argument(
            'client_id',
            help='The ClientProfile UUID to log the deployment against.',
        )
        parser.add_argument(
            '--title',
            default='Site updated',
            help='Entry title (default: "Site updated").',
        )
        parser.add_argument(
            '--description',
            default='',
            help='Optional longer description.',
        )

    def handle(self, *args, **options):
        client_id = options['client_id']
        try:
            client = ClientProfile.objects.get(id=client_id)
        except (ClientProfile.DoesNotExist, ValidationError, ValueError):
            raise CommandError(f'No client found with id {client_id!r}.')

        entry = SiteChangelogEntry.objects.create(
            client=client,
            change_type='deployment',
            title=options['title'],
            description=options['description'],
            is_client_visible=True,
            date_of_change=timezone.localdate(),
        )
        self.stdout.write(self.style.SUCCESS(
            f'Logged deployment for {client.firm_name}: "{entry.title}" '
            f'({entry.date_of_change}).'
        ))
