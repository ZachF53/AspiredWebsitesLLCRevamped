"""Refresh Aspired's Google rating, review count and reviews now.

    python manage.py sync_google_reviews

Runs daily via public.tasks.sync_google_reviews_task; see
public/google_reviews.py.
"""

from django.core.management.base import BaseCommand, CommandError

from public import google_reviews


class Command(BaseCommand):
    help = "Sync Aspired's own Google reviews from the Places API."

    def handle(self, *args, **opts):
        try:
            self.stdout.write(google_reviews.sync())
        except google_reviews.GoogleReviewsError as err:
            raise CommandError(str(err))
