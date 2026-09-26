"""
Inbound sync event handlers (Moonieful → Aspired).

Each handler takes the parsed JSON bundle and returns the affected
Account (or None). Every locally-originated save sets
`instance._from_sync = True` so the outbound signal does not echo the
change back to Moonieful (loop prevention).

Field ownership follows CLAUDE.md. Moonieful owns the identity and the
intake answers, which are account-level; Aspired owns the build stages,
which are per site. So a synced client materialises as one Account plus
one Website, and `stage` / `moonieful_handoff_at` are written to the
Website rather than to the account.

Bundle shape is a locked cross-repo contract — see docs/sync_contract.md.
Every event ships the FULL current bundle (never a partial diff), nested as
`bundle['client']` (with account/login fields nested again under
`bundle['client']['account']`), plus top-level `stage_history`, `documents`,
and `intake` lists.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from clients.emails import send_maintenance_handoff_email
from clients.models import (
    ClientDocument,
    IntakeResponse,
)
from clients.account_models import Account, WebsiteStageLog, Website
from sync.token_utils import generate_handoff_token

logger = logging.getLogger(__name__)


def _unique_username(email):
    User = get_user_model()
    base = (email.split('@')[0] or 'client')[:140]
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f'{base}{suffix}'
        suffix += 1
    return username


def _parse_dt(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return dt


def _contact_name(account_data):
    first = (account_data.get('first_name') or '').strip()
    last = (account_data.get('last_name') or '').strip()
    return f'{first} {last}'.strip()


def _moonieful_website(profile):
    """The Website tied to THIS account's Moonieful referral, or None.

    Deliberately filtered on moonieful_referred=True rather than just
    "the account's oldest website" (the previous behavior): one Account can
    own multiple Websites (a pre-existing or later Aspired-direct build,
    plus a Moonieful referral), and picking the wrong one silently
    overwrites a project's stage/business_type/handoff status with data
    that belongs to a completely different build.
    """
    return (
        profile.websites.filter(moonieful_referred=True)
        .order_by('created_at')
        .first()
    )


_DIRECTIONS = {'to_client', 'from_client'}

# Optional top-level bundle keys carrying the rest of what Miki can see on
# her side. Stored verbatim in Website.moonieful_extra, one entry per key,
# and rendered read-only on the v2 Moonieful tab. See docs/sync_contract.md
# "Optional extension keys". Unknown keys are ignored, never stored.
EXTRA_KEYS = (
    'tasks', 'meetings', 'contracts', 'invoices', 'approvals',
    'change_requests', 'recommendations', 'completion', 'testimonial',
    'activity', 'stage_details',
)


def _upsert_document(site, ref, *, label, description='', direction='to_client',
                     filename='', category='', updated_at=None,
                     deleted=None, visible_to_client=None):
    """Create or update one Moonieful-owned ClientDocument row.

    Keyed on moonieful_document_id (== the file_ref the file endpoint is
    called with). Metadata is last-writer-wins on Moonieful's updated_at:
    a stale bundle never rolls a newer label/description back. The file
    itself is never touched here — it arrives via /api/sync/file/<ref>/.
    """
    if direction not in _DIRECTIONS:
        direction = 'to_client'
    incoming = _parse_dt(updated_at) if isinstance(updated_at, str) else updated_at

    fields = {
        'label': (label or '')[:255],
        'description': description or '',
        'direction': direction,
        'original_filename': (filename or '')[:255],
        'category': (category or '')[:30],
    }
    if incoming is not None:
        fields['moonieful_updated_at'] = incoming
    if deleted is not None:
        fields['moonieful_deleted'] = bool(deleted)
    if visible_to_client is not None:
        fields['moonieful_visible_to_client'] = bool(visible_to_client)

    doc = ClientDocument.objects.filter(moonieful_document_id=ref).first()
    if doc is None:
        ClientDocument.objects.create(
            moonieful_document_id=ref, website_new=site, **fields)
        return
    if (incoming is not None and doc.moonieful_updated_at is not None
            and doc.moonieful_updated_at >= incoming):
        return
    changed = []
    for name, value in fields.items():
        if getattr(doc, name) != value:
            setattr(doc, name, value)
            changed.append(name)
    if doc.website_new_id is None:
        doc.website_new = site
        changed.append('website_new')
    if changed:
        doc.save(update_fields=changed + ['updated_at'])


def _upsert_documents(site, docs):
    """Upsert every document in a bundle's `documents` list (or the
    optional `extra_files` list, which has the same shape plus `ref`).

    Safe to call with the full list every time. Moonieful's `direction`
    is honoured (a file the client uploaded on her side is `from_client`
    here too), and description / filename / category / deleted /
    visible_to_client are kept rather than dropped.
    """
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        ref = doc.get('file_ref') or doc.get('ref') or doc.get('id')
        if not ref:
            continue
        deleted = doc.get('is_deleted')
        if deleted is None and 'deleted_at' in doc:
            deleted = bool(doc.get('deleted_at'))
        _upsert_document(
            site, ref,
            label=doc.get('label') or doc.get('filename') or 'Moonieful document',
            description=doc.get('description') or '',
            direction=doc.get('direction') or 'to_client',
            filename=doc.get('filename') or '',
            category=doc.get('category') or '',
            updated_at=doc.get('updated_at'),
            deleted=deleted,
            visible_to_client=doc.get('visible_to_client'),
        )


def _upsert_intake_file_documents(site, intake):
    """A ClientDocument row for every file_ref inside `intake[].answers[]`.

    An intake answer's file (a logo upload, say) is referenced by
    file_ref just like a top-level document, and Moonieful's file
    transport (iter_bundle_files) streams it to the same
    /api/sync/file/<file_ref>/ endpoint either way. Without a matching
    ClientDocument row created here first, that endpoint 404s on every
    intake attachment.

    The client answered the intake, so these are `from_client`.
    """
    for entry in intake or []:
        if not isinstance(entry, dict):
            continue
        for answer in entry.get('answers') or []:
            if not isinstance(answer, dict):
                continue
            file_ref = answer.get('file_ref')
            if not file_ref:
                continue
            _upsert_document(
                site, file_ref,
                label=answer.get('question_text') or 'Moonieful intake file',
                description=entry.get('form_title') or '',
                direction='from_client',
                category='intake',
                updated_at=entry.get('submitted_at'),
            )


def _apply_extra(site, bundle):
    """Copy the optional extension keys into Website.moonieful_extra.

    Each key present in the bundle overwrites; absent keys are left as
    they were (so a Moonieful build that never sends them wipes nothing).
    Files attached to those records arrive as `extra_files` and become
    ordinary ClientDocuments.
    """
    extra = dict(site.moonieful_extra or {})
    changed = False
    for key in EXTRA_KEYS:
        if key in bundle:
            extra[key] = bundle[key]
            changed = True
    if changed:
        site.moonieful_extra = extra
        site._from_sync = True
        site.save(update_fields=['moonieful_extra', 'updated_at'])
    if bundle.get('extra_files'):
        _upsert_documents(site, bundle['extra_files'])


def _apply_intake(site, intake_data):
    intake, _ = IntakeResponse.objects.get_or_create(website_new=site)
    intake._from_sync = True
    intake.moonieful_intake_raw = intake_data or []
    update = ['moonieful_intake_raw', 'updated_at']
    # Moonieful owns intake for the clients it refers — the answers live
    # in moonieful_intake_raw, not the typed fields — so Aspired's own
    # intake form is never the gate for these sites.
    if not intake.completed:
        intake.completed = True
        intake.completed_at = timezone.now()
        update += ['completed', 'completed_at']
    intake.save(update_fields=update)
    _upsert_intake_file_documents(site, intake_data)


def handle_client_created(bundle):
    """Create (or link) a client synced over from Moonieful."""
    data = bundle.get('client') or {}
    account_data = data.get('account') or {}
    email = (account_data.get('email') or '').strip().lower()
    if not email:
        raise ValueError('client_created: bundle is missing client email')

    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    conflict = False
    if user is None:
        user = User(username=_unique_username(email), email=email)
        password_hash = account_data.get('password_hash')
        if password_hash:
            # Moonieful sends an already-hashed password — store it directly,
            # NOT via set_password() (which would hash the hash).
            user.password = password_hash
        else:
            user.set_unusable_password()
        user.save()
    else:
        # An account already exists for this email — link to it, flag the
        # conflict, and never overwrite the existing password.
        conflict = True

    name = data.get('business_name') or 'Moonieful Client'
    profile, _ = Account.objects.get_or_create(
        user=user, defaults={'name': name},
    )
    profile.name = data.get('business_name') or profile.name
    profile.contact_name = _contact_name(account_data) or profile.contact_name
    profile.phone = data.get('phone') or profile.phone
    profile.moonieful_client_id = data.get('moonieful_client_id')
    profile.synced_from_moonieful = True
    profile.sync_conflict_flagged = conflict
    profile.last_synced_at = timezone.now()
    profile._from_sync = True
    profile.save()

    # This account may already have an unrelated Website (a pre-existing
    # Aspired-direct build) — never adopt that one. Only ever adopt a
    # website already flagged as this account's Moonieful referral.
    site = _moonieful_website(profile)
    if site is None:
        site = Website(account=profile, name=profile.name)
    site.name = profile.name or site.name
    site.url = data.get('website') or site.url
    # Never inherit the Law Firm default for a Moonieful client
    # (CLAUDE.md): the type is set by hand once someone knows it.
    site.business_type = ''
    site.stage = 'intake'
    site.package = 'moonieful_referred'
    site.moonieful_referred = True
    site.moonieful_package = data.get('moonieful_package') or ''
    site.moonieful_stage_history = bundle.get('stage_history') or []
    # Moonieful owns intake — never park a referred site behind Aspired's
    # own intake form (clients/decorators.py `pending_intake` gate).
    if site.onboarding_status == 'pending_intake':
        site.onboarding_status = 'intake_complete'
    site._from_sync = True
    site.save()

    _apply_intake(site, bundle.get('intake'))
    _upsert_documents(site, bundle.get('documents'))
    _apply_extra(site, bundle)

    logger.info('sync: created client %s from Moonieful (%s)', profile.pk,
                profile.moonieful_client_id)
    return profile


def handle_client_updated(bundle):
    """Update Moonieful-owned fields on an already-synced client."""
    data = bundle.get('client') or {}
    account_data = data.get('account') or {}
    profile = Account.objects.filter(
        moonieful_client_id=data.get('moonieful_client_id')
    ).first()
    if profile is None:
        raise ValueError('client_updated: no client for that Moonieful id')

    incoming = _parse_dt(data.get('updated_at'))
    if incoming is not None and profile.updated_at and profile.updated_at > incoming:
        logger.info('sync: skipping stale client_updated for %s', profile.pk)
        return profile

    site = _moonieful_website(profile)

    contact_name = _contact_name(account_data)
    if contact_name:
        profile.contact_name = contact_name
    if data.get('business_name'):
        profile.name = data['business_name']
    if data.get('phone'):
        profile.phone = data['phone']
    if data.get('website') and site is not None:
        site._from_sync = True
        site.url = data['website']
        site.save(update_fields=['url', 'updated_at'])
    if account_data.get('email'):
        profile.user.email = account_data['email'].strip().lower()
        profile.user.save(update_fields=['email'])

    if site is not None:
        site_fields = []
        if 'stage_history' in bundle:
            site.moonieful_stage_history = bundle.get('stage_history') or []
            site_fields.append('moonieful_stage_history')
        if data.get('moonieful_package'):
            site.moonieful_package = data['moonieful_package']
            site_fields.append('moonieful_package')
        if site_fields:
            site._from_sync = True
            site.save(update_fields=site_fields + ['updated_at'])
        if 'intake' in bundle:
            _apply_intake(site, bundle['intake'])
        _upsert_documents(site, bundle.get('documents'))
        _apply_extra(site, bundle)

    profile.last_synced_at = timezone.now()
    profile._from_sync = True
    profile.save()
    return profile


def handle_project_complete(bundle):
    """Moonieful marked the project complete — hand off to Aspired maintenance."""
    data = bundle.get('client') or {}
    moonieful_id = data.get('moonieful_client_id')
    profile = Account.objects.filter(
        moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('project_complete: no client for that Moonieful id')

    site = _moonieful_website(profile)
    if site is None:
        raise ValueError(
            'project_complete: account has no Moonieful-referred website to hand off')

    old_stage = site.stage
    site.stage = 'live'
    site.moonieful_handoff_at = timezone.now()
    site._from_sync = True
    site.save()

    # WebsiteStageLog, not ProjectStageLog.
    #
    # The portal's Activity Log and project timeline both read
    # `stage_logs`, which is the WebsiteStageLog reverse accessor (see
    # clients/views `_project_timeline`). A ProjectStageLog row is not on
    # that relation, so the single most significant event in a
    # Moonieful-referred client's project -- "your site is live, handed
    # off" -- never appeared on their timeline. It is also the legacy
    # model, so the row went away with the drop regardless.
    WebsiteStageLog.objects.create(
        website=site,
        from_stage=old_stage,
        to_stage='live',
        note='Project handed off from Moonieful.',
        set_by='sync',
    )

    token = generate_handoff_token(str(profile.id))
    handoff_url = f'{settings.SITE_BASE_URL}/maintenance/start/?token={token}'
    send_maintenance_handoff_email(profile, handoff_url)
    logger.info('sync: project_complete handoff for client %s', profile.pk)
    return profile


def handle_document_added(bundle):
    """Register documents Moonieful added — files follow via /api/sync/file/.

    Moonieful always ships the full current `documents` list, not a single
    new one, so this reuses the same upsert path client_created uses —
    already idempotent per document via moonieful_document_id.
    """
    data = bundle.get('client') or {}
    moonieful_id = data.get('moonieful_client_id')
    profile = Account.objects.filter(
        moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('document_added: no client for that Moonieful id')

    docs = bundle.get('documents') or []
    if not docs:
        raise ValueError('document_added: bundle has no documents')

    site = _moonieful_website(profile)
    if site is None:
        raise ValueError(
            'document_added: account has no Moonieful-referred website')

    _upsert_documents(site, docs)
    _apply_extra(site, bundle)
    return profile


def handle_stage_changed(bundle):
    """Mirror Moonieful's own stage history for Miki's visibility only.

    This never touches `site.stage` — that's Aspired's own build-stage
    field and is Aspired-owned per the field-ownership map. It only updates
    the read-only `moonieful_stage_history` snapshot.
    """
    data = bundle.get('client') or {}
    moonieful_id = data.get('moonieful_client_id')
    profile = Account.objects.filter(
        moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('stage_changed: no client for that Moonieful id')

    site = _moonieful_website(profile)
    if site is None:
        raise ValueError(
            'stage_changed: account has no Moonieful-referred website')

    site.moonieful_stage_history = bundle.get('stage_history') or []
    site._from_sync = True
    site.save(update_fields=['moonieful_stage_history', 'updated_at'])
    _apply_extra(site, bundle)
    return profile


def handle_revision_created(bundle):
    """Reserved — Moonieful has no revision feature, so this is a no-op."""
    logger.info('sync: revision_created received — ignored (no Moonieful revisions)')
    return None


HANDLERS = {
    'client_created': handle_client_created,
    'client_updated': handle_client_updated,
    'project_complete': handle_project_complete,
    'document_added': handle_document_added,
    'stage_changed': handle_stage_changed,
    'revision_created': handle_revision_created,
}
