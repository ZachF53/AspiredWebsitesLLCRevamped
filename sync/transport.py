"""
Outbound HTTP transport for the sync bridge (Aspired → Moonieful).

Two payload shapes exist:
- Plain status events (stage_changed, maintenance_activated) — one signed
  JSON POST to Moonieful's inbound endpoint.
- document_added — the same JSON POST, followed by a streamed file POST to
  Moonieful's file-transfer endpoint. Both legs must succeed for the job to
  count as delivered — see deliver_document for why that matters.
"""

import json
import time

import requests
from django.conf import settings

from clients.models import ClientDocument
from sync.security import SIGNATURE_VERSION, sign


def _envelope(job):
    """payload_snapshot is already shaped as {'moonieful_client_id': ...,
    'data': {...}} (see sync/signals.py) — the spread below supplies those
    two top-level keys, matching what Moonieful's handlers read
    (docs/sync_contract.md)."""
    return {
        'schema_version': 1,
        'source_site': 'aspired',
        'event_type': job.event_type,
        'event_id': str(job.event_id),
        **(job.payload_snapshot or {}),
    }


def _signed_headers(signed_bytes, content_type, extra=None):
    timestamp = str(int(time.time()))
    headers = {
        'Content-Type': content_type,
        'X-Sync-Version': SIGNATURE_VERSION,
        'X-Sync-Timestamp': timestamp,
        'X-Sync-Signature': sign(timestamp, signed_bytes),
    }
    if extra:
        headers.update(extra)
    return headers


def deliver_json(job, url):
    """POST one job's JSON envelope with a freshly computed signature +
    timestamp. Used for stage_changed/maintenance_activated, and for the
    metadata leg of document_added."""
    body = json.dumps(_envelope(job), sort_keys=True).encode()
    headers = _signed_headers(body, 'application/json')
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=20)
    except requests.RequestException as exc:
        return False, str(exc)
    if resp.status_code == 200:
        return True, ''
    return False, f'HTTP {resp.status_code}: {resp.text[:200]}'


def deliver_document(job, inbound_url, file_url):
    """Deliver a document_added job: the JSON metadata leg first, then the
    file body — only if the metadata leg succeeds.

    Both legs must return 200 for the job to be considered sent. This
    matters because Moonieful's own dispatch() treats an unrecognized
    event_type as an ignored no-op that still returns HTTP 200 (confirmed
    by reading her sync/handlers.py) — until her side adds a document_added
    handler, the metadata leg alone would look like success on its own. If
    delivery only checked that first response, the job would be marked
    'sent' forever while the file never actually arrived. Requiring both
    legs to succeed means a job with nothing to receive it on the other end
    fails cleanly, retries on the existing backoff, and eventually reaches
    the admin alert email in run_sync.py — the correct, already-built
    failure path — instead of silently losing the file.

    The ClientDocument is looked up fresh at delivery time (not read out of
    the frozen payload_snapshot) so a job sitting in the retry queue still
    picks up the current file state, and a deleted document fails cleanly
    instead of raising.
    """
    ok, error = deliver_json(job, inbound_url)
    if not ok:
        return False, error

    if not file_url:
        return False, 'MOONIEFUL_SYNC_FILE_URL not configured'

    data = (job.payload_snapshot or {}).get('data') or {}
    document_id = data.get('document_id')
    if not document_id:
        return False, 'job payload has no document_id'

    document = ClientDocument.objects.filter(id=document_id).first()
    if document is None:
        return False, f'document {document_id} no longer exists'
    if not document.file:
        return False, f'document {document_id} has no file attached'

    try:
        size = document.file.size
    except Exception as exc:  # noqa: BLE001 — missing on disk, not a crash
        return False, f'file unreadable: {exc}'

    filename = document.file.name.rsplit('/', 1)[-1]
    url = f"{file_url.rstrip('/')}/{document_id}/"
    # Signed over the URL + size, not the file bytes — hashing a large
    # upload twice (once to sign, once to send) is wasteful, and the file
    # endpoint's authority comes from the same shared secret either way.
    # Matches sync/views.py::sync_file, which verifies inbound files the
    # same way.
    signed_over = f'{url}\n{size}'.encode('utf-8')
    headers = _signed_headers(
        signed_over, 'application/octet-stream',
        extra={'X-Sync-Filename': filename, 'Content-Length': str(size)},
    )

    try:
        document.file.open('rb')
        try:
            # Passing the open file object (not .read()) as data= lets
            # requests stream it in chunks instead of loading the whole
            # thing into memory — the same discipline Moonieful's own
            # sync/transport.py::send_files documents adopting after an
            # OOM incident from .read()-ing a whole upload on a small
            # droplet.
            resp = requests.post(
                url, data=document.file, headers=headers, timeout=300)
        finally:
            document.file.close()
    except requests.RequestException as exc:
        return False, f'file transfer failed: {exc}'
    except Exception as exc:  # noqa: BLE001
        return False, f'could not open file: {exc}'

    if resp.status_code == 200:
        return True, ''
    return False, f'file transfer HTTP {resp.status_code}: {resp.text[:200]}'
