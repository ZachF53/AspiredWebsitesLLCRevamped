"""
Inbound sync endpoints (Moonieful → Aspired).

Both endpoints authenticate with an HMAC-SHA256 signature over the raw
request body, keyed by MOONIEFUL_SYNC_SECRET (the same value must be
configured on Miki's server). The inbound event endpoint additionally
requires a fresh X-Sync-Timestamp (within 5 minutes) to block replays.
"""

import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.contrib.auth import login
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from clients.emails import send_maintenance_handoff_email
from clients.models import ClientDocument, ClientProfile
from sync.handlers import HANDLERS
from sync.models import SyncLog
from sync.token_utils import generate_handoff_token, validate_handoff_token

logger = logging.getLogger(__name__)

TIMESTAMP_TOLERANCE = 300  # seconds — reject events older/newer than 5 minutes


def _timestamp_fresh(raw_ts):
    if not raw_ts:
        return False
    try:
        ts = int(float(raw_ts))
    except (TypeError, ValueError):
        return False
    return abs(time.time() - ts) <= TIMESTAMP_TOLERANCE


def _signature_valid(body, provided_sig):
    secret = settings.MOONIEFUL_SYNC_SECRET
    if not secret or not provided_sig:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided_sig)


@csrf_exempt
@require_POST
def sync_inbound(request):
    """POST /api/sync/inbound/ — receive a Moonieful sync event."""
    raw = request.body
    timestamp = request.META.get('HTTP_X_SYNC_TIMESTAMP', '')
    signature = request.META.get('HTTP_X_SYNC_SIGNATURE', '')

    if not _timestamp_fresh(timestamp):
        return JsonResponse({'error': 'stale or missing timestamp'}, status=403)

    if not _signature_valid(raw, signature):
        SyncLog.objects.create(
            source_site='moonieful', event_type='unknown',
            payload_received={}, status='failed',
            error_message='HMAC signature mismatch',
        )
        return JsonResponse({'error': 'invalid signature'}, status=403)

    try:
        bundle = json.loads(raw)
    except ValueError:
        return JsonResponse({'error': 'invalid JSON'}, status=400)
    if not isinstance(bundle, dict):
        return JsonResponse({'error': 'invalid payload'}, status=400)
    if bundle.get('schema_version') != 1:
        return JsonResponse({'error': 'unsupported schema_version'}, status=400)

    event_type = bundle.get('event_type', '')
    log = SyncLog.objects.create(
        source_site='moonieful', event_type=event_type,
        payload_received=bundle, status='processed',
    )

    handler = HANDLERS.get(event_type)
    if handler is None:
        log.status = 'skipped'
        log.error_message = f'No handler for event_type "{event_type}"'
        log.save(update_fields=['status', 'error_message', 'updated_at'])
        return JsonResponse({'error': 'unknown event_type'}, status=400)

    try:
        client = handler(bundle)
    except Exception as exc:
        logger.exception('sync inbound handler failed for %s', event_type)
        log.status = 'failed'
        log.error_message = str(exc)
        log.save(update_fields=['status', 'error_message', 'updated_at'])
        return JsonResponse({'error': 'handler error'}, status=400)

    return JsonResponse({
        'status': 'ok',
        'aspired_client_id': str(client.id) if client else None,
    }, status=200)


@csrf_exempt
@require_POST
def sync_file(request, document_id):
    """POST /api/sync/file/<document_id>/ — receive a document's file body."""
    raw = request.body  # read first so multipart parsing reuses the cached body
    signature = request.META.get('HTTP_X_SYNC_SIGNATURE', '')
    if not _signature_valid(raw, signature):
        return JsonResponse({'error': 'invalid signature'}, status=403)

    document = ClientDocument.objects.filter(
        moonieful_document_id=document_id
    ).first()
    if document is None:
        return JsonResponse({'error': 'document not found'}, status=404)

    upload = request.FILES.get('file')
    if upload is None:
        return JsonResponse({'error': 'no file provided'}, status=400)

    document.file.save(upload.name, upload, save=True)
    return JsonResponse({'status': 'ok'}, status=200)


# ── Maintenance handoff (Part 8) ────────────────────────────────────────────

def _maintenance_tiers():
    """Active maintenance ServiceTiers — the DB is the source of truth."""
    from billing.pricing_models import ServiceTier
    return ServiceTier.get_active('maintenance')


def maintenance_start(request):
    """
    GET /maintenance/start/?token=...  — validate the handoff token, start a
    maintenance-scoped session, and show the plan picker.
    POST                               — plan selection, or a new-link request.
    """
    if request.method == 'POST':
        return _maintenance_post(request)

    token = request.GET.get('token', '')
    if not token:
        return render(request, 'sync/token_expired.html',
                      {'reason': 'missing'}, status=400)

    client_id = validate_handoff_token(token)
    client = ClientProfile.objects.filter(id=client_id).first() if client_id else None
    if client is None:
        return render(request, 'sync/token_expired.html', {})

    # Session-limited login — scoped to maintenance selection only.
    login(request, client.user,
          backend='django.contrib.auth.backends.ModelBackend')
    request.session['maintenance_flow_only'] = True

    return render(request, 'sync/maintenance_start.html', {
        'client': client,
        'plans': _maintenance_tiers(),
    })


def _maintenance_post(request):
    # Case 1 — a new-link request submitted from the expired-token page.
    request_email = (request.POST.get('request_email') or '').strip().lower()
    if request_email:
        client = ClientProfile.objects.filter(
            user__email__iexact=request_email,
            synced_from_moonieful=True,
            maintenance_active=False,
        ).first()
        if client is not None:
            token = generate_handoff_token(str(client.id))
            url = f'{settings.SITE_BASE_URL}/maintenance/start/?token={token}'
            send_maintenance_handoff_email(client, url)
        # Same response either way — never reveal whether the email exists.
        return render(request, 'sync/token_expired.html', {'link_sent': True})

    # Case 2 — plan selection.
    plan_slug = request.POST.get('plan', '')
    if not request.session.get('maintenance_flow_only') or not request.user.is_authenticated:
        return redirect(settings.LOGIN_URL)
    client = ClientProfile.objects.filter(user=request.user).first()
    if client is None:
        return redirect(settings.LOGIN_URL)

    from billing.pricing_models import ServiceTier
    tier = ServiceTier.objects.filter(
        slug=plan_slug, category='maintenance', is_active=True,
    ).first()
    if tier is None:
        return render(request, 'sync/maintenance_start.html', {
            'client': client, 'plans': _maintenance_tiers(),
            'error': 'Please choose a plan to continue.',
        })

    # Create the recurring subscription via Stripe (best effort). Final
    # activation (maintenance_active=True) happens on the invoice.paid webhook.
    try:
        from billing.stripe_helpers import create_maintenance_subscription
        create_maintenance_subscription(client, tier.slug)
    except Exception:
        logger.exception('Maintenance subscription not created for %s', client.pk)

    return render(request, 'sync/maintenance_start.html', {
        'client': client,
        'plans': _maintenance_tiers(),
        'selected_plan': tier,
    })
