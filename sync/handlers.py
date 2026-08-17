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
    ProjectStageLog,
)
from clients.account_models import Account, Website
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


def handle_client_created(bundle):
    """Create (or link) a client synced over from Moonieful."""
    data = bundle.get('client') or {}
    email = (data.get('email') or '').strip().lower()
    if not email:
        raise ValueError('client_created: bundle is missing client email')

    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    conflict = False
    if user is None:
        user = User(username=_unique_username(email), email=email)
        password_hash = data.get('password_hash')
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

    name = (data.get('firm_name') or data.get('name')
            or 'Moonieful Client')
    profile, _ = Account.objects.get_or_create(
        user=user, defaults={'name': name},
    )
    profile.name = data.get('firm_name') or profile.name
    profile.contact_name = data.get('name') or profile.contact_name
    profile.phone = data.get('phone') or profile.phone
    profile.moonieful_client_id = data.get('id')
    profile.synced_from_moonieful = True
    profile.sync_conflict_flagged = conflict
    profile.last_synced_at = timezone.now()
    profile._from_sync = True
    profile.save()

    # The build is the site's. Adopt the Website the account-autocreate
    # signal has already made rather than adding a second one.
    site = profile.websites.order_by('created_at').first()
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
    site.moonieful_package = data.get('package') or ''
    site.moonieful_stage_history = bundle.get('stage_history') or []
    site._from_sync = True
    site.save()

    intake, _ = IntakeResponse.objects.get_or_create(website_new=site)
    intake._from_sync = True
    intake.moonieful_intake_raw = bundle.get('intake') or {}
    intake.save(update_fields=['moonieful_intake_raw', 'updated_at'])

    for doc in bundle.get('documents') or []:
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

    logger.info('sync: created client %s from Moonieful (%s)', profile.pk,
                profile.moonieful_client_id)
    return profile


def handle_client_updated(bundle):
    """Update Moonieful-owned fields on an already-synced client."""
    data = bundle.get('client') or {}
    profile = Account.objects.filter(
        moonieful_client_id=data.get('id')
    ).first()
    if profile is None:
        raise ValueError('client_updated: no client for that Moonieful id')

    incoming = _parse_dt(bundle.get('updated_at'))
    if incoming is not None and profile.updated_at and profile.updated_at > incoming:
        logger.info('sync: skipping stale client_updated for %s', profile.pk)
        return profile

    site = profile.websites.order_by('created_at').first()

    if data.get('name'):
        profile.contact_name = data['name']
    if data.get('firm_name'):
        profile.name = data['firm_name']
    if data.get('phone'):
        profile.phone = data['phone']
    if data.get('website') and site is not None:
        site._from_sync = True
        site.url = data['website']
        site.save(update_fields=['url', 'updated_at'])
    if data.get('email'):
        profile.user.email = data['email'].strip().lower()
        profile.user.save(update_fields=['email'])

    if 'intake' in bundle and site is not None:
        intake = IntakeResponse.objects.filter(website_new=site).first()
        if intake is not None:
            intake._from_sync = True
            intake.moonieful_intake_raw = bundle['intake']
            intake.save(update_fields=['moonieful_intake_raw', 'updated_at'])

    profile.last_synced_at = timezone.now()
    profile._from_sync = True
    profile.save()
    return profile


def handle_project_complete(bundle):
    """Moonieful marked the project complete — hand off to Aspired maintenance."""
    data = bundle.get('client') or {}
    moonieful_id = data.get('id') or bundle.get('moonieful_client_id')
    profile = Account.objects.filter(
        moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('project_complete: no client for that Moonieful id')

    site = profile.websites.order_by('created_at').first()
    if site is None:
        raise ValueError(
            'project_complete: account has no website to hand off')

    old_stage = site.stage
    site.stage = 'live'
    site.moonieful_handoff_at = timezone.now()
    site._from_sync = True
    site.save()

    ProjectStageLog.objects.create(
        website_new=site,
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
    """Register a document Moonieful added — the file follows via /api/sync/file/."""
    data = bundle.get('client') or {}
    moonieful_id = data.get('id') or bundle.get('moonieful_client_id')
    profile = Account.objects.filter(
        moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('document_added: no client for that Moonieful id')

    doc = bundle.get('document') or {}
    if not doc.get('id'):
        raise ValueError('document_added: bundle is missing document id')
    site = profile.websites.order_by('created_at').first()
    ClientDocument.objects.get_or_create(
        moonieful_document_id=doc.get('id'),
        defaults={
            'website_new': site,
            'direction': 'to_client',
            'label': doc.get('label') or 'Moonieful document',
        },
    )
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
    'revision_created': handle_revision_created,
}
