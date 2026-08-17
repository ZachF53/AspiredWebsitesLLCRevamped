"""
Day 3/7/14 maintenance handoff follow-ups for Moonieful-referred clients.

Cron: daily. See CLAUDE.md — no Celery for the sync bridge.

Iterates Websites. The handoff is per site: `moonieful_handoff_at`,
`maintenance_active` and `handoff_followup_sent` all live on Website,
because Miki hands over a finished site and the client buys maintenance
for that site. The account-level version could only ever chase one of
them, so a client who received two sites was nagged about the first and
never about the second.

The signed token still carries the ACCOUNT id: it exists to log the
client in, scoped to plan selection, and the login is an account-level
fact.
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from clients.account_models import Website
from clients.emails import send_maintenance_handoff_email
from sync.token_utils import generate_handoff_token

logger = logging.getLogger(__name__)

FOLLOWUP_DAYS = [3, 7, 14]


class Command(BaseCommand):
    help = 'Send Day 3/7/14 maintenance handoff follow-up emails (cron: daily).'

    def handle(self, *args, **options):
        now = timezone.now()
        candidates = (
            Website.objects
            .filter(
                account__synced_from_moonieful=True,
                maintenance_active=False,
                moonieful_handoff_at__isnull=False,
            )
            .select_related('account')
        )

        total = 0
        for site in candidates:
            account = site.account
            if account is None:
                logger.error(
                    'send_handoff_followups: website %s has no account — '
                    'cannot issue a login token, skipped', site.pk)
                continue

            days_since = (now - site.moonieful_handoff_at).days
            sent = dict(site.handoff_followup_sent or {})
            changed = False

            for day in FOLLOWUP_DAYS:
                key = f'day{day}'
                if days_since >= day and key not in sent:
                    token = generate_handoff_token(str(account.id))
                    url = (f'{settings.SITE_BASE_URL}'
                           f'/maintenance/start/?token={token}')
                    send_maintenance_handoff_email(
                        account, url, followup_day=day)
                    sent[key] = now.isoformat()
                    changed = True
                    total += 1

            if changed:
                site.handoff_followup_sent = sent
                site.save(update_fields=[
                    'handoff_followup_sent', 'updated_at'])

        self.stdout.write(
            f'send_handoff_followups: sent {total} follow-up email(s).'
        )
