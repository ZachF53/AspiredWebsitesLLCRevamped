"""
Inbound sync endpoints (Moonieful → Aspired).

Both endpoints authenticate with the versioned, timestamp-bound HMAC scheme
in sync/security.py, keyed by MOONIEFUL_SYNC_SECRET (the same value must be
configured on Miki's server). See docs/sync_contract.md for the exact wire
format this must match — it is a locked cross-repo contract, not a local
convention.
"""

import json
import logging

from django.conf import settings
from django.contrib.auth import login
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from clients.emails import send_maintenance_handoff_email
from clients.account_models import Account, Website
from clients.models import ClientDocument
from sync.handlers import HANDLERS
from sync.models import SyncLog
from sync.security import secret_configured, timestamp_fresh, verify
from sync.token_utils import generate_handoff_token, validate_handoff_token

logger = logging.getLogger(__name__)


def _account_for_token(subject_id):
    """Resolve a handoff token's subject to an Account, or None.

    Tokens live for 48 hours and the ones issued before this cutover carry
    a legacy ClientProfile id, not an Account id. Both are accepted, so a
    client who received a link yesterday is not met with "this link has
    expired" through no fault of their own.

    The legacy branch queries Account on its own ``legacy_client_profile``
    column rather than importing ClientProfile, so this stays correct
    without being a legacy read.
    """
    if not subject_id:
        return None
    account = Account.objects.filter(id=subject_id).first()
    if account is not None:
        return account
    return Account.objects.filter(
        legacy_client_profile_id=subject_id).first()


def _authenticated(raw_body, timestamp, signature):
    """True if the signature covers THIS body AND THIS timestamp.

    Fails closed: with no secret configured, verify() refuses rather than
    checking against an empty key (see sync/security.py).
    """
    if not secret_configured():
        return False
    if not timestamp_fresh(timestamp):
        return False
    return verify(timestamp, raw_body, signature)


@csrf_exempt
@require_POST
def sync_inbound(request):
    """POST /api/sync/inbound/ — receive a Moonieful sync event."""
    raw = request.body
    timestamp = request.META.get('HTTP_X_SYNC_TIMESTAMP', '')
    signature = request.META.get('HTTP_X_SYNC_SIGNATURE', '')

    if not _authenticated(raw, timestamp, signature):
        SyncLog.objects.create(
            source_site='moonieful', event_type='unknown',
            payload_received={}, status='failed',
            error_message='HMAC signature/timestamp check failed',
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
    event_id = str(bundle.get('event_id') or '')

    # Idempotency. The sender retries, so the same event can arrive more
    # than once (e.g. a successful delivery whose response got lost to a
    # network blip); without this, a retry re-runs the handler and
    # re-sends emails / duplicates log rows. Returning ok rather than an
    # error is deliberate — from the sender's point of view the event HAS
    # been applied, and an error would make it retry forever.
    if event_id:
        already = SyncLog.objects.filter(
            source_site='moonieful', event_id=event_id, status='processed',
        ).first()
        if already is not None:
            return JsonResponse({
                'status': 'ok', 'detail': 'already applied', 'duplicate': True,
            })

    log = SyncLog.objects.create(
        source_site='moonieful', event_type=event_type, event_id=event_id,
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
    raw = request.body
    timestamp = request.META.get('HTTP_X_SYNC_TIMESTAMP', '')
    signature = request.META.get('HTTP_X_SYNC_SIGNATURE', '')
    signed_over = f'{request.build_absolute_uri()}\n{len(raw)}'.encode('utf-8')
    if not _authenticated(signed_over, timestamp, signature):
        return JsonResponse({'error': 'invalid signature'}, status=403)

    document = ClientDocument.objects.filter(
        moonieful_document_id=document_id
    ).first()
    if document is None:
        return JsonResponse({'error': 'document not found'}, status=404)

    # Moonieful streams the file as the raw request body
    # (Content-Type: application/octet-stream), not a multipart upload —
    # request.FILES is never populated for that, so this used to reject
    # every real file Moonieful ever sent. The filename travels in a
    # header the sender controls, so only its base name is trusted (no
    # path traversal), and the reconstructed upload runs through the same
    # type/size validation a multipart upload would have gotten.
    import os

    from django.core.files.uploadedfile import SimpleUploadedFile

    raw_name = request.headers.get('X-Sync-Filename', '') or f'{document_id}.bin'
    filename = os.path.basename(raw_name.replace('\\', '/')).strip() or f'{document_id}.bin'

    SYNC_MAX_SIZE = 50 * 1024 * 1024  # 50 MB
    # Widened to match what Moonieful's own upload form actually lets her
    # send (portal/forms.py PORTAL_UPLOAD_ALLOWED_EXT on her side) — the
    # narrower list below used to accept the metadata for a document Miki
    # sent and then 400 the file body itself for anything not on it,
    # silently leaving a fileless row behind. Archives (.zip etc.) and
    # .html/.htm are deliberately NOT included even though her form allows
    # them: an archive's contents are invisible to an extension check, and
    # .html is only safe on her side because her serving view forces a
    # Content-Disposition: attachment header — Aspired serves MEDIA_URL
    # directly with no such view in front of it.
    SYNC_ALLOWED_EXTS = {
        # docs
        'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md', 'pages', 'epub',
        'xls', 'xlsx', 'csv', 'ods', 'numbers',
        'ppt', 'pptx', 'odp', 'key',
        # images
        'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg',
        'bmp', 'tif', 'tiff', 'heic', 'heif', 'avif',
        # audio / video
        'mp4', 'mov', 'webm', 'avi', 'mkv', 'wmv', 'm4v', 'mpg', 'mpeg',
        'mp3', 'wav', 'm4a', 'm4b', 'aac', 'flac', 'ogg', 'oga', 'aif', 'aiff',
        # design / source files
        'psd', 'ai', 'sketch', 'fig', 'xd', 'indd', 'eps',
        'afdesign', 'afphoto', 'procreate', 'dwg', 'dxf',
        # fonts — brand handovers routinely include the licensed typeface
        'ttf', 'otf', 'woff', 'woff2',
    }
    if len(raw) > SYNC_MAX_SIZE:
        return JsonResponse(
            {'error': 'file too large (50 MB max)'}, status=400)
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    if ext not in SYNC_ALLOWED_EXTS:
        return JsonResponse(
            {'error': f'file type ".{ext}" not allowed'}, status=400)

    upload = SimpleUploadedFile(filename, raw)
    document.file.save(filename, upload, save=True)
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
    client = _account_for_token(client_id)
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
        # "Has not bought maintenance yet" is a per-site fact, so it is
        # asked of the account's websites rather than of the account.
        client = Account.objects.filter(
            user__email__iexact=request_email,
            synced_from_moonieful=True,
            websites__maintenance_active=False,
        ).distinct().first()
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
    client = Account.objects.filter(user=request.user).first()
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
