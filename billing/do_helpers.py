"""
DigitalOcean Droplet provisioning helpers.

One Droplet per client (CLAUDE.md). provision_client_droplet() is called by
the provision_droplet_task Celery task after a deposit payment clears.
"""

import logging
import time

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.text import slugify

logger = logging.getLogger(__name__)

DO_API = 'https://api.digitalocean.com/v2'
DROPLET_REGION = 'nyc1'          # closest region serving TX/GA
DROPLET_SIZE = 's-1vcpu-1gb'     # $6/month
PROVISION_TIMEOUT = 300          # seconds — max wait for status=active
POLL_INTERVAL = 10               # seconds between status polls


class DONotConfigured(RuntimeError):
    """Raised when a DO API call is attempted without DO_API_TOKEN."""


def _headers():
    if not settings.DO_API_TOKEN:
        raise DONotConfigured('DO_API_TOKEN is not set in .env')
    return {
        'Authorization': f'Bearer {settings.DO_API_TOKEN}',
        'Content-Type': 'application/json',
    }


def droplet_name_for(client):
    """Naming convention: clientname-prod (lowercase, hyphens)."""
    return f'{slugify(client.firm_name)}-prod'


def _public_ip(droplet):
    for net in (droplet.get('networks') or {}).get('v4') or []:
        if net.get('type') == 'public':
            return net.get('ip_address')
    return None


def provision_client_droplet(client):
    """
    Create the client's Droplet from the base snapshot, poll until it is
    active, store the ID/IP on the ClientProfile, and notify admin.
    Returns the droplet dict. Raises on API failure.
    """
    headers = _headers()
    name = droplet_name_for(client)
    payload = {
        'name': name,
        'region': DROPLET_REGION,
        'size': DROPLET_SIZE,
        'image': settings.DO_BASE_SNAPSHOT_ID,
        'tags': ['aspired-websites', 'client'],
    }
    resp = requests.post(
        f'{DO_API}/droplets', json=payload, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    droplet = resp.json()['droplet']
    droplet_id = droplet['id']
    logger.info('DO: created droplet %s (%s) for %s', droplet_id, name, client.pk)

    # Poll until the Droplet is active and has a public IP.
    deadline = time.time() + PROVISION_TIMEOUT
    while time.time() < deadline:
        if droplet.get('status') == 'active' and _public_ip(droplet):
            break
        time.sleep(POLL_INTERVAL)
        poll = requests.get(
            f'{DO_API}/droplets/{droplet_id}', headers=headers, timeout=30,
        )
        poll.raise_for_status()
        droplet = poll.json()['droplet']

    ip = _public_ip(droplet)
    client.do_droplet_id = str(droplet_id)
    client.do_droplet_ip = ip or None
    client.do_droplet_created_at = timezone.now()
    client.save(update_fields=[
        'do_droplet_id', 'do_droplet_ip', 'do_droplet_created_at', 'updated_at',
    ])
    _notify_admin(client, droplet_id, ip)
    return droplet


def snapshot_client_droplet(client, label):
    """Take a snapshot of the client's Droplet. Returns the action ID."""
    headers = _headers()
    if not client.do_droplet_id:
        raise ValueError(f'{client.firm_name} has no Droplet to snapshot.')
    resp = requests.post(
        f'{DO_API}/droplets/{client.do_droplet_id}/actions',
        json={'type': 'snapshot', 'name': label},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['action']['id']


def transfer_snapshot(snapshot_id, target_email):
    """Transfer a snapshot to another DO account — used for offboarding."""
    headers = _headers()
    resp = requests.post(
        f'{DO_API}/images/{snapshot_id}/actions',
        json={'type': 'transfer', 'transfer_to_account': target_email},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['action']['id']


def destroy_client_droplet(client):
    """Destroy the client's Droplet (payment-failure Day 30). Snapshot retained."""
    if not client.do_droplet_id:
        return
    headers = _headers()
    resp = requests.delete(
        f'{DO_API}/droplets/{client.do_droplet_id}', headers=headers, timeout=30,
    )
    resp.raise_for_status()
    logger.info('DO: destroyed droplet %s for %s', client.do_droplet_id, client.pk)
    client.do_droplet_id = ''
    client.do_droplet_ip = None
    client.save(update_fields=['do_droplet_id', 'do_droplet_ip', 'updated_at'])


def _notify_admin(client, droplet_id, ip):
    send_mail(
        subject=f'Droplet created for {client.firm_name} — IP: {ip or "pending"}',
        message=(
            f'A DigitalOcean Droplet has been provisioned.\n\n'
            f'Client:  {client.firm_name}\n'
            f'Droplet: {droplet_id} ({droplet_name_for(client)})\n'
            f'IP:      {ip or "pending"}\n'
        ),
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )
