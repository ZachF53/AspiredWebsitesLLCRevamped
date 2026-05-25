"""
Inbound sync event handlers (Moonieful → Aspired).

Each handler takes the parsed JSON bundle and returns the affected
ClientProfile (or None). Every locally-originated save sets
`instance._from_sync = True` so the outbound signal does not echo the
change back to Moonieful (loop prevention).
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from clients.emails import send_maintenance_handoff_email
from clients.models import (
    ClientDocument,
    ClientProfile,
    IntakeResponse,
    ProjectStageLog,
)
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

    profile, _ = ClientProfile.objects.get_or_create(
        user=user,
        defaults={'firm_name': data.get('firm_name') or data.get('name') or 'Moonieful Client'},
    )
    profile.firm_name = data.get('firm_name') or profile.firm_name
    profile.contact_name = data.get('name') or profile.contact_name
    profile.phone = data.get('phone') or profile.phone
    profile.website = data.get('website') or profile.website
    profile.business_type = ''  # never inherit the Law Firm default
    profile.moonieful_client_id = data.get('id')
    profile.synced_from_moonieful = True
    profile.moonieful_package = data.get('package') or ''
    profile.package = 'moonieful_referred'
    profile.sync_conflict_flagged = conflict
    profile.last_synced_at = timezone.now()
    # Project fields are now flat on ClientProfile (2026-05-25 refactor).
    profile.stage = 'intake'
    profile.moonieful_stage_history = bundle.get('stage_history') or []
    profile._from_sync = True
    profile.save()

    intake, _ = IntakeResponse.objects.get_or_create(client=profile)
    intake._from_sync = True
    intake.moonieful_intake_raw = bundle.get('intake') or {}
    intake.save(update_fields=['moonieful_intake_raw', 'updated_at'])

    for doc in bundle.get('documents') or []:
        if not doc.get('id'):
            continue
        ClientDocument.objects.get_or_create(
            moonieful_document_id=doc.get('id'),
            defaults={
                'client': profile,
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
    profile = ClientProfile.objects.filter(
        moonieful_client_id=data.get('id')
    ).first()
    if profile is None:
        raise ValueError('client_updated: no client for that Moonieful id')

    incoming = _parse_dt(bundle.get('updated_at'))
    if incoming is not None and profile.updated_at and profile.updated_at > incoming:
        logger.info('sync: skipping stale client_updated for %s', profile.pk)
        return profile

    if data.get('name'):
        profile.contact_name = data['name']
    if data.get('firm_name'):
        profile.firm_name = data['firm_name']
    if data.get('phone'):
        profile.phone = data['phone']
    if data.get('website'):
        profile.website = data['website']
    if data.get('email'):
        profile.user.email = data['email'].strip().lower()
        profile.user.save(update_fields=['email'])

    if 'intake' in bundle:
        intake = IntakeResponse.objects.filter(client=profile).first()
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
    profile = ClientProfile.objects.filter(moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('project_complete: no client for that Moonieful id')

    # Project fields are now flat on ClientProfile (2026-05-25 refactor).
    old_stage = profile.stage
    profile.stage = 'live'
    profile.moonieful_handoff_at = timezone.now()
    profile._from_sync = True
    profile.save()

    ProjectStageLog.objects.create(
        client=profile,
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
    profile = ClientProfile.objects.filter(moonieful_client_id=moonieful_id).first()
    if profile is None:
        raise ValueError('document_added: no client for that Moonieful id')

    doc = bundle.get('document') or {}
    if not doc.get('id'):
        raise ValueError('document_added: bundle is missing document id')
    ClientDocument.objects.get_or_create(
        moonieful_document_id=doc.get('id'),
        defaults={
            'client': profile,
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
