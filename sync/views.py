"""
Inbound sync endpoints (Moonieful → Aspired).

Both endpoints authenticate with the versioned, timestamp-bound HMAC scheme
in sync/security.py, keyed by MOONIEFUL_SYNC_SECRET (the same value must be
configured on Miki's server). See docs/sync_contract.md for the exact wire
format this must match — it is a locked cross-repo contract, not a local
convention.
"""

import copy
import json
import logging
import os
import tempfile

from django.conf import settings
from django.contrib.auth import login
from django.core.files import File
from django.db import transaction
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


REDACTED = '[redacted]'

# Every file type Moonieful's upload form lets her send, minus archives
# (their contents are invisible to an extension check) and .html/.htm.
# SVG is allowed: files are only ever served by the auth-checked download
# views, forced to Content-Disposition: attachment with a sandbox CSP and
# nosniff, so a scripted SVG never renders on this origin.
SYNC_ALLOWED_EXTS = frozenset({
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
})

_CHUNK = 64 * 1024


def _redact(bundle):
    """A copy of the bundle safe to keep in SyncLog.

    Moonieful ships the client's Django password hash on every event. The
    handler needs it once (to create the login); the audit log must never
    hold it.
    """
    safe = copy.deepcopy(bundle)
    client = safe.get('client')
    account = client.get('account') if isinstance(client, dict) else None
    if isinstance(account, dict) and account.get('password_hash'):
        account['password_hash'] = REDACTED
    return safe


def _reject(status, message, *, event_type='unknown', payload=None):
    SyncLog.objects.create(
        source_site='moonieful', event_type=event_type,
        payload_received=payload or {}, status='failed',
        error_message=message,
    )
    return JsonResponse({'error': message}, status=status)


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
        return _reject(400, 'invalid JSON')
    if not isinstance(bundle, dict):
        return _reject(400, 'invalid payload')
    if bundle.get('schema_version') != 1:
        return _reject(400, 'unsupported schema_version',
                       event_type=str(bundle.get('event_type') or 'unknown')[:100],
                       payload=_redact(bundle))

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

    handler = HANDLERS.get(event_type)
    if handler is None:
        SyncLog.objects.create(
            source_site='moonieful', event_type=event_type, event_id=event_id,
            payload_received=_redact(bundle), status='skipped',
            error_message=f'No handler for event_type "{event_type}"',
        )
        return JsonResponse({'error': 'unknown event_type'}, status=400)

    # The handler runs in its own transaction: a failure halfway through
    # (account saved, website not) rolls back to nothing rather than
    # leaving a half-synced client. The log row is written after, with
    # the real outcome, so a crash can no longer leave it saying
    # "processed" for an event that was never applied.
    try:
        with transaction.atomic():
            client = handler(bundle)
    except Exception as exc:
        logger.exception('sync inbound handler failed for %s', event_type)
        SyncLog.objects.create(
            source_site='moonieful', event_type=event_type, event_id=event_id,
            payload_received=_redact(bundle), status='failed',
            error_message=str(exc),
        )
        return JsonResponse({'error': 'handler error'}, status=400)

    SyncLog.objects.create(
        source_site='moonieful', event_type=event_type, event_id=event_id,
        payload_received=_redact(bundle), status='processed',
        account_new=client if isinstance(client, Account) else None,
    )
    return JsonResponse({
        'status': 'ok',
        'aspired_client_id': str(client.id) if client else None,
    }, status=200)


def _file_log(document_id, status, message='', *, document=None, filename='', size=None):
    account = None
    if document is not None and document.website_new_id:
        account = document.website_new.account
    SyncLog.objects.create(
        source_site='moonieful', event_type='file_received',
        payload_received={
            'document_id': str(document_id),
            'filename': filename,
            'size': size,
        },
        status=status, error_message=message, account_new=account,
    )


@csrf_exempt
@require_POST
def sync_file(request, document_id):
    """POST /api/sync/file/<document_id>/ — receive a document's file body.

    Moonieful streams the file as the raw request body
    (Content-Type: application/octet-stream). The signature covers
    "<absolute url>\n<byte length>", so it is checked against the declared
    Content-Length BEFORE a single byte is read, and the body is then
    streamed to a temp file in chunks and held to that exact length.

    This never touches request.body: that path loads the whole file into
    memory and is capped by DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB by
    default), which used to reject every real brand file with a 400
    before the size check here ever ran.
    """
    timestamp = request.META.get('HTTP_X_SYNC_TIMESTAMP', '')
    signature = request.META.get('HTTP_X_SYNC_SIGNATURE', '')
    max_size = settings.SYNC_MAX_FILE_SIZE

    try:
        declared = int(request.META.get('CONTENT_LENGTH') or '')
    except ValueError:
        return JsonResponse({'error': 'Content-Length required'}, status=411)
    if declared < 0:
        return JsonResponse({'error': 'invalid Content-Length'}, status=400)

    signed_over = f'{request.build_absolute_uri()}\n{declared}'.encode('utf-8')
    if not _authenticated(signed_over, timestamp, signature):
        return JsonResponse({'error': 'invalid signature'}, status=403)

    raw_name = request.headers.get('X-Sync-Filename', '') or f'{document_id}.bin'
    filename = os.path.basename(raw_name.replace('\\', '/')).strip() or f'{document_id}.bin'

    document = ClientDocument.objects.filter(
        moonieful_document_id=document_id
    ).select_related('website_new__account').first()
    if document is None:
        _file_log(document_id, 'failed', 'document not found (metadata not synced yet)',
                  filename=filename, size=declared)
        return JsonResponse({'error': 'document not found'}, status=404)

    if declared > max_size:
        msg = f'file too large ({max_size // (1024 * 1024)} MB max)'
        _file_log(document_id, 'failed', msg, document=document,
                  filename=filename, size=declared)
        return JsonResponse({'error': msg}, status=413)

    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    if ext not in SYNC_ALLOWED_EXTS:
        msg = f'file type ".{ext}" not allowed'
        _file_log(document_id, 'failed', msg, document=document,
                  filename=filename, size=declared)
        return JsonResponse({'error': msg}, status=400)

    with tempfile.TemporaryFile() as tmp:
        received = 0
        while True:
            chunk = request.read(min(_CHUNK, declared - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > declared:
                break
            tmp.write(chunk)
        if received != declared:
            msg = f'body length {received} does not match Content-Length {declared}'
            _file_log(document_id, 'failed', msg, document=document,
                      filename=filename, size=declared)
            return JsonResponse({'error': 'body does not match Content-Length'}, status=400)

        tmp.seek(0)
        # A re-send replaces the stored file rather than piling up
        # name_XXXX.ext duplicates next to it.
        if document.file:
            document.file.delete(save=False)
        document.file.save(filename, File(tmp, name=filename), save=False)
        if not document.original_filename:
            document.original_filename = filename[:255]
        document.save(update_fields=['file', 'original_filename', 'updated_at'])

    _file_log(document_id, 'processed', document=document,
              filename=filename, size=declared)
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
