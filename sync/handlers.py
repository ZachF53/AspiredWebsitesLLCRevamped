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


def _upsert_documents(site, docs):
    """get_or_create every document in a bundle's `documents` list against
    the given Website. Safe to call with the full list every time — already
    idempotent via moonieful_document_id."""
    for doc in docs or []:
        if not doc.get('id'):
            continue
        ClientDocument.objects.get_or_create(
            moonieful_document_id=doc.get('id'),
            defaults={
                'website_new': site,
                'direction': 'to_client',
                'label': doc.get('label') or 'Moonieful document',
            },
        )


def _upsert_intake_file_documents(site, intake):
    """A ClientDocument row for every file_ref inside `intake[].answers[]`.

    An intake answer's file (a logo upload, say) is referenced by
    file_ref just like a top-level document, and Moonieful's file
    transport (iter_bundle_files) streams it to the same
    /api/sync/file/<file_ref>/ endpoint either way. Without a matching
    ClientDocument row created here first, that endpoint 404s on every
    intake attachment — found by actually running the bridge end-to-end
    rather than trusting the handler tests alone, since the test fixture
    never set a real file_ref.
    """
    for entry in intake or []:
        for answer in entry.get('answers') or []:
            file_ref = answer.get('file_ref')
            if not file_ref:
                continue
            ClientDocument.objects.get_or_create(
                moonieful_document_id=file_ref,
                defaults={
                    'website_new': site,
                    'direction': 'to_client',
                    'label': answer.get('question_text') or 'Moonieful intake file',
                },
            )


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
    site._from_sync = True
    site.save()

    intake, _ = IntakeResponse.objects.get_or_create(website_new=site)
    intake._from_sync = True
    intake.moonieful_intake_raw = bundle.get('intake') or {}
    intake.save(update_fields=['moonieful_intake_raw', 'updated_at'])

    _upsert_documents(site, bundle.get('documents'))
    _upsert_intake_file_documents(site, bundle.get('intake'))

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

    if 'intake' in bundle and site is not None:
        intake = IntakeResponse.objects.filter(website_new=site).first()
        if intake is not None:
            intake._from_sync = True
            intake.moonieful_intake_raw = bundle['intake']
            intake.save(update_fields=['moonieful_intake_raw', 'updated_at'])
        _upsert_intake_file_documents(site, bundle['intake'])

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
