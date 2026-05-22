"""
Reporting Celery tasks — uptime monitoring, GBP NAP sync, conversion-drop
alerts, and weekly keyword rank checks.

External integrations (Google Business Profile, Google Search Console) are
not yet connected — those tasks degrade gracefully and log/record the gap
rather than failing.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Admin notification ──────────────────────────────────────────────────────

def send_admin_alert(subject, message):
    """Email an operational alert to the admin notification address."""
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.EMAIL_FROM_NO_REPLY,
            recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('send_admin_alert failed: %s', subject)


# ── Part 1: Uptime monitoring ───────────────────────────────────────────────

@shared_task
def check_client_uptime():
    """Ping every active, launched client site. Scheduled every 5 minutes."""
    import requests
    from clients.models import ClientProfile, UptimeRecord, UptimeAlert

    # do_droplet_ip is a GenericIPAddressField — when blank it is stored as
    # NULL (never ''), so isnull=False alone selects clients with a server.
    active_clients = ClientProfile.objects.filter(
        status='active', do_droplet_ip__isnull=False,
    )

    checked = 0
    for client in active_clients:
        project = client.projects.filter(stage='live').first()
        if not project or not project.live_url:
            continue

        url = project.live_url
        if not url.startswith('http'):
            url = f'https://{url}'

        try:
            start = timezone.now()
            response = requests.get(
                url, timeout=15, allow_redirects=True,
                headers={'User-Agent': 'AspiredWebsites-Monitor/1.0'},
            )
            response_time = int(
                (timezone.now() - start).total_seconds() * 1000)
            is_up = response.status_code < 500

            UptimeRecord.objects.create(
                client=client,
                response_time_ms=response_time,
                status_code=response.status_code,
                is_up=is_up,
                error_message='' if is_up else f'HTTP {response.status_code}',
            )

            if is_up:
                UptimeAlert.objects.filter(
                    client=client, is_resolved=False,
                ).update(is_resolved=True, resolved_at=timezone.now())
            else:
                check_and_fire_alert(client)

        except requests.RequestException as exc:
            UptimeRecord.objects.create(
                client=client,
                response_time_ms=None,
                status_code=None,
                is_up=False,
                error_message=str(exc)[:200],
            )
            check_and_fire_alert(client)
        checked += 1

    return f'Checked {checked} client site(s).'


def check_and_fire_alert(client):
    """
    Open a downtime alert after 3 consecutive failed checks — once per
    outage, so a long outage does not spam the admin on every check.
    """
    from clients.models import UptimeRecord, UptimeAlert

    recent = list(
        UptimeRecord.objects.filter(client=client).order_by('-checked_at')[:3]
    )
    if len(recent) < 3 or not all(not r.is_up for r in recent):
        return

    if UptimeAlert.objects.filter(client=client, is_resolved=False).exists():
        return  # an alert is already open for this outage

    UptimeAlert.objects.create(
        client=client, consecutive_failures=3, alert_sent=True)

    project = client.projects.filter(stage='live').first()
    live_url = project.live_url if project else '(unknown)'
    send_admin_alert(
        subject=f'🔴 Site Down: {client.firm_name}',
        message=(
            f'{client.firm_name} has been down for 3 consecutive checks.\n'
            f'Domain: {live_url}\n'
            f'Check: /admin-dashboard/clients/{client.id}/uptime/'
        ),
    )


# ── Part 2: GBP NAP sync ────────────────────────────────────────────────────

GBP_NOT_CONNECTED = 'GBP not connected'


def _gbp_is_connected(client):
    """
    True when this client has a usable Google Business Profile connection.

    Phase 4 social OAuth is not built yet, so this is always False today —
    check_gbp_sync records an informational row rather than a real compare.
    """
    return False


@shared_task
def check_gbp_sync():
    """
    Check NAP (name/phone/address/website) consistency between each client's
    site and their Google Business Profile. Scheduled weekly (Mon 9am).

    GBP OAuth is a Phase 4 dependency — until it lands this records a single
    informational GBPSyncCheck per client instead of a real comparison.
    """
    from clients.models import ClientProfile
    from .models import GBPSyncCheck

    clients = ClientProfile.objects.filter(status='active')
    recorded = 0
    for client in clients:
        project = client.projects.filter(stage='live').first()
        if not project:
            continue

        if not _gbp_is_connected(client):
            GBPSyncCheck.objects.create(
                client=client,
                field_name='website',
                website_value=GBP_NOT_CONNECTED,
                gbp_value='Connect GBP in Phase 4 setup',
                is_mismatch=False,
            )
            recorded += 1
            continue

        # When GBP OAuth lands: scrape NAP fields from project.live_url,
        # fetch the same fields from the GBP API, and record a GBPSyncCheck
        # per field — flagging is_mismatch and notifying the admin on a diff.

    return f'Recorded GBP sync status for {recorded} client(s).'


# ── Part 3: Keyword rank tracking ───────────────────────────────────────────

@shared_task
def check_keyword_ranks():
    """
    Refresh ranking positions for every active tracked keyword. Scheduled
    weekly (Mon 7am).

    Live positions come from the Google Search Console API, which requires
    per-client OAuth (Phase 4). Until that is connected this is a safe
    no-op — staff can enter KeywordRankRecord rows manually via the admin.
    """
    from .models import TrackedKeyword

    active = TrackedKeyword.objects.filter(is_active=True).count()
    logger.info(
        'check_keyword_ranks: %s active keyword(s); GSC API not connected — '
        'no automatic ranks fetched.', active,
    )
    return f'{active} active keyword(s); GSC not connected.'


# ── Part 4: Conversion-drop alerts ──────────────────────────────────────────

@shared_task
def check_conversion_drops():
    """
    Compare this month's form submissions to last month's per client.
    A drop of 30%+ raises an admin alert. Scheduled on the 2nd at 8am.
    """
    from clients.models import ClientProfile
    from .models import ConversionEvent

    now = timezone.now()
    this_month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)

    alerted = 0
    for client in ClientProfile.objects.filter(status='active'):
        this_month = ConversionEvent.objects.filter(
            client=client, event_type='form_submit',
            event_timestamp__gte=this_month_start,
        ).count()
        last_month = ConversionEvent.objects.filter(
            client=client, event_type='form_submit',
            event_timestamp__gte=last_month_start,
            event_timestamp__lt=this_month_start,
        ).count()

        if last_month == 0 or this_month == 0:
            continue

        drop_pct = ((last_month - this_month) / last_month) * 100
        if drop_pct >= 30:
            send_admin_alert(
                subject=f'⚠ Conversion Drop: {client.firm_name}',
                message=(
                    f'Form submissions dropped {drop_pct:.0f}% this month.\n'
                    f'Last month: {last_month}\n'
                    f'This month: {this_month}\n'
                    f'Check: /admin-dashboard/clients/{client.id}/conversions/'
                ),
            )
            alerted += 1

    return f'Conversion-drop check complete — {alerted} alert(s) sent.'
