"""
Data backfill — copy every Project's fields onto its ClientProfile,
and populate the new `client` FKs on IntakeResponse, RevisionRequest,
and ProjectStageLog.

After this migration:
  - Every ClientProfile that had a Project has stage / package /
    payment_status / launch_date / etc. populated directly on the
    profile row.
  - Every IntakeResponse / RevisionRequest / ProjectStageLog has its
    new `client` FK set (sourced from project.client).

The Project table is NOT touched. Both old + new state coexist
during the dual-write window. Phase 2 will drop the Project table.

Conflict resolution rules:
  - ClientProfile.package: only filled from Project.package if the
    client doesn't already have one set (don't clobber a maintenance
    plan choice).
  - For clients with multiple projects (none currently): the LIVE
    project wins; if none is live, the most-recent project wins.
"""

from django.db import migrations


def backfill_client_from_projects(apps, schema_editor):
    ClientProfile = apps.get_model('clients', 'ClientProfile')
    Project = apps.get_model('clients', 'Project')
    IntakeResponse = apps.get_model('clients', 'IntakeResponse')
    RevisionRequest = apps.get_model('clients', 'RevisionRequest')
    ProjectStageLog = apps.get_model('clients', 'ProjectStageLog')

    # ── 1. ClientProfile fields backfill ─────────────────────────────
    for client in ClientProfile.objects.all():
        # Canonical project: live first, else most recent
        project = (
            Project.objects.filter(client=client, stage='live').first()
            or Project.objects.filter(client=client).order_by(
                '-created_at').first()
        )
        if project is None:
            # No project for this client (auxiliary vault profile etc).
            # Leave fields at their model defaults; nothing to copy.
            continue

        client.stage = project.stage or 'intake'
        client.staging_url = project.staging_url or ''
        client.launch_date = project.launch_date
        client.support_window_ends = project.support_window_ends
        client.payment_status = (
            project.payment_status or 'awaiting_deposit')
        client.deposit_paid_at = project.deposit_paid_at
        client.final_paid_at = project.final_paid_at
        client.revision_count = project.revision_count or 0
        client.revision_limit = project.revision_limit or 2
        client.moonieful_handoff_at = project.moonieful_handoff_at
        client.moonieful_stage_history = (
            project.moonieful_stage_history or [])

        # `package`: don't clobber a maintenance plan choice on the
        # client; only fill if the client doesn't already have one.
        if (not client.package) and project.package:
            client.package = project.package

        client.save()

    # ── 2. Repoint IntakeResponse.client ────────────────────────────
    for intake in IntakeResponse.objects.filter(client__isnull=True):
        if intake.project_id:
            intake.client_id = intake.project.client_id
            intake.save()

    # ── 3. Repoint RevisionRequest.client ───────────────────────────
    for rev in RevisionRequest.objects.filter(client__isnull=True):
        if rev.project_id:
            rev.client_id = rev.project.client_id
            rev.save()

    # ── 4. Repoint ProjectStageLog.client ───────────────────────────
    for log in ProjectStageLog.objects.filter(client__isnull=True):
        if log.project_id:
            log.client_id = log.project.client_id
            log.save()


def noop_reverse(apps, schema_editor):
    """Reverse is a noop — data exists in both places, just stops
    auto-syncing. To fully revert, restore from a pre-migration DB
    snapshot."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0026_clientprofile_deposit_paid_at_and_more'),
    ]

    operations = [
        migrations.RunPython(
            backfill_client_from_projects, noop_reverse),
    ]
