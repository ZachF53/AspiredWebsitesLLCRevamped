"""
send_handoff_followups — Day 3 / 7 / 14 maintenance handoff reminders.

Scheduled daily via cron. Targets Moonieful-referred clients who were handed
off but have not yet started a maintenance plan. Each reminder carries a
freshly signed handoff token; sends are recorded in handoff_followup_sent so
each day fires only once.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from clients.emails import send_maintenance_handoff_email
from clients.models import ClientProfile
from sync.token_utils import generate_handoff_token

FOLLOWUP_DAYS = [3, 7, 14]


class Command(BaseCommand):
    help = 'Send Day 3/7/14 maintenance handoff follow-up emails (cron: daily).'

    def handle(self, *args, **options):
        now = timezone.now()
        candidates = ClientProfile.objects.filter(
            synced_from_moonieful=True,
            maintenance_active=False,
            projects__moonieful_handoff_at__isnull=False,
        ).distinct()

        total = 0
        for client in candidates:
            project = (
                client.projects
                .filter(moonieful_handoff_at__isnull=False)
                .order_by('-moonieful_handoff_at')
                .first()
            )
            if project is None:
                continue
            days_since = (now - project.moonieful_handoff_at).days
            sent = dict(client.handoff_followup_sent or {})
            changed = False

            for day in FOLLOWUP_DAYS:
                key = f'day{day}'
                if days_since >= day and key not in sent:
                    token = generate_handoff_token(str(client.id))
                    url = f'{settings.SITE_BASE_URL}/maintenance/start/?token={token}'
                    send_maintenance_handoff_email(client, url, followup_day=day)
                    sent[key] = now.isoformat()
                    changed = True
                    total += 1

            if changed:
                client.handoff_followup_sent = sent
                client.save(update_fields=['handoff_followup_sent', 'updated_at'])

        self.stdout.write(
            f'send_handoff_followups: sent {total} follow-up email(s).'
        )
