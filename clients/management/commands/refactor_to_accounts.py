"""
refactor_to_accounts — Phase B backfill.

Walks every ``ClientProfile`` and:

  1. Creates / refreshes a matching ``Account`` (idempotent via
     ``Account.legacy_client_profile``).
  2. Creates / refreshes a single ``Website`` under that Account if the
     client has substantive build data (a Project row, a live URL, a
     droplet, a non-default stage, a launch date, an active maintenance
     sub, or a build-package code). Auxiliary vault-only profiles are
     intentionally Account-only — they get no Website.
  3. Repoints every dependent FK to ``account_new`` / ``website_new``
     on the rows that already FK the legacy client / project.

Safe to re-run. Phase C readers will start preferring the ``_new`` FKs
once it's confirmed the backfill is clean. Phase D drops the legacy
columns + models.

Usage:
  python manage.py refactor_to_accounts --dry-run
  python manage.py refactor_to_accounts

Flags:
  --dry-run   Print the would-be mutations; touch nothing.
  --verbose   Per-row decisions (otherwise just the summary).
"""

import sys
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction


# ── Helpers ──────────────────────────────────────────────────────────────

def _legacy_project_for(client):
    """
    Pick the canonical Project to source per-build fields from for a
    legacy client. Preference: a Project with stage='live'; failing
    that, the most recently created Project. Returns ``None`` if the
    client has no Project rows (auxiliary vault profiles).
    """
    from clients.models import Project
    p = Project.objects.filter(client=client, stage='live').first()
    if p:
        return p
    return Project.objects.filter(client=client).order_by(
        '-created_at').first()


def _client_has_website_data(client):
    """
    True if this legacy ClientProfile carries enough build state to
    justify creating a Website. Auxiliary vault-only profiles (no
    project, no URL, no droplet, default stage) return False — they
    become Account-only.
    """
    if client.projects.exists():
        return True
    if (client.website or '').strip():
        return True
    if (getattr(client, 'do_droplet_id', '') or '').strip():
        return True
    if (client.package or '').strip():
        return True
    if client.launch_date or client.maintenance_active:
        return True
    if client.stage and client.stage != 'intake':
        return True
    return False


def _make_account_from_client(client):
    """Build the kwargs dict for Account.objects.update_or_create."""
    onboarding_status = 'pending_setup'
    legacy = getattr(client, 'onboarding_status', '') or ''
    if legacy in ('pending_intake', 'onboarding_complete'):
        # Account onboarding is now just WHOIS + PIN — anything past
        # that on the old single-table flow is "complete" at the
        # account level.
        onboarding_status = 'complete'

    return {
        'user': client.user,
        'name': client.firm_name or client.user.email,
        'contact_name': client.contact_name or '',
        'phone': client.phone or '',
        'address': client.address or '',
        'city': client.city or '',
        'state': client.state or '',
        'zip_code': client.zip_code or '',
        'country': 'US',
        'status': client.status or 'active',
        'is_tester': bool(client.is_tester),
        'internal_notes': client.internal_notes or '',
        'stripe_customer_id': client.stripe_customer_id or '',
        'preferred_contact_method': (
            client.preferred_contact_method or 'email'),
        'notify_on_stage_change': bool(client.notify_on_stage_change),
        'notify_on_invoice': True,
        'notify_on_scan_complete': True,
        'onboarding_status': onboarding_status,
        'onboarding_complete': bool(client.onboarding_complete),
        'client_pin_hash': client.client_pin_hash or '',
        'client_pin_salt': client.client_pin_salt or b'',
        'client_pin_set': bool(client.client_pin_set),
        'client_pin_failed_attempts': (
            client.client_pin_failed_attempts or 0),
        'client_pin_lockout_until': client.client_pin_lockout_until,
        'moonieful_client_id': client.moonieful_client_id,
        'synced_from_moonieful': bool(client.synced_from_moonieful),
        'last_synced_at': client.last_synced_at,
        'sync_conflict_flagged': bool(client.sync_conflict_flagged),
    }


def _make_website_from_client(client, account, project):
    """
    Build the kwargs dict for Website.objects.update_or_create. Source
    of truth: ClientProfile (since Phase 1/2 of the Project drop
    consolidated the per-build fields onto ClientProfile). The Project
    row, if any, is only used as the idempotency anchor.
    """
    # Per-website onboarding state — only meaningful for non-live builds.
    if (client.stage or '') == 'live':
        onboarding_status = 'complete'
    elif (client.onboarding_status or '') == 'pending_intake':
        onboarding_status = 'pending_intake'
    else:
        onboarding_status = 'intake_complete'

    # Pull maintenance subscription ID — the legacy field name was
    # `stripe_subscription_id`. Hosting sub already lives under
    # `stripe_hosting_subscription_id` and stays there.
    maintenance_sub_id = (
        getattr(client, 'stripe_subscription_id', '') or '')

    return {
        'account': account,
        'name': client.firm_name,
        # slug is auto-generated on save() if blank.
        'business_type': client.business_type or '',
        'url': client.website or '',
        'staging_url': client.staging_url or '',
        'status': 'active',
        'stage': client.stage or 'intake',
        'package': client.package or '',
        'onboarding_status': onboarding_status,
        'do_droplet_id': client.do_droplet_id or '',
        'do_droplet_ip': client.do_droplet_ip,
        'do_droplet_created_at': client.do_droplet_created_at,
        'do_droplet_name': '',
        'launch_date': client.launch_date,
        'support_window_ends': client.support_window_ends,
        'payment_status': client.payment_status or 'awaiting_deposit',
        'deposit_paid_at': client.deposit_paid_at,
        'final_paid_at': client.final_paid_at,
        'revision_count': client.revision_count or 0,
        'revision_limit': client.revision_limit or 2,
        'revisions_reset_at': client.revisions_reset_at,
        'moonieful_referred': bool(
            project.moonieful_referred if project else False),
        'moonieful_handoff_at': client.moonieful_handoff_at,
        'moonieful_stage_history': (
            client.moonieful_stage_history or []),
        'moonieful_package': client.moonieful_package or '',
        'handoff_followup_sent': client.handoff_followup_sent or {},
        'maintenance_upsell_log': client.maintenance_upsell_log or {},
        'stripe_hosting_subscription_id': (
            client.stripe_hosting_subscription_id or ''),
        'stripe_maintenance_subscription_id': maintenance_sub_id,
        'stripe_invoice_id': client.stripe_invoice_id or '',
        'maintenance_active': bool(client.maintenance_active),
        'maintenance_started_at': client.maintenance_started_at,
        'maintenance_cancelled_at': None,
        'session_recording_enabled': bool(
            client.session_recording_enabled),
        'auto_send_scan_reports': bool(client.auto_send_scan_reports),
        'needs_admin_review_at': client.needs_admin_review_at,
        'admin_reviewed_at': client.admin_reviewed_at,
        'testimonial_requested_at': client.testimonial_requested_at,
        'testimonial_received': bool(client.testimonial_received),
        'testimonial_url': client.testimonial_url or '',
        'legacy_project': project,
    }


# ── Repointing dependent FKs ──────────────────────────────────────────────

# Each entry: (model_path, client_attr, account_attr, website_attr)
# Either account_attr or website_attr (or both) is set per row. None
# means that side of the FK isn't being backfilled here.
DEPENDENT_REPOINTS = [
    # clients/
    ('clients.ProjectStageLog',     'client', None,          'website_new'),
    ('clients.IntakeResponse',      'client', None,          'website_new'),
    ('clients.RevisionRequest',     'client', None,          'website_new'),
    ('clients.ClientDocument',      'client', None,          'website_new'),
    ('clients.SupportTicket',       'client', 'account_new', 'website_new'),
    ('clients.Contract',            'client', None,          'website_new'),
    ('clients.SiteChangelogEntry',  'client', None,          'website_new'),
    ('clients.UptimeRecord',        'client', None,          'website_new'),
    ('clients.UptimeAlert',         'client', None,          'website_new'),
    ('clients.ClientHealthScore',   'client', None,          'website_new'),
    ('clients.ReferralLink',        'client', 'account_new', None),
    ('clients.CaseStudy',           'client', None,          'website_new'),
    ('clients.IntelligenceReport',  'client', None,          'website_new'),
    ('clients.IntelligenceSuggestion', 'client', None,       'website_new'),
    ('clients.AnnualReport',        'client', None,          'website_new'),
    ('clients.ClientCompetitor',    'client', None,          'website_new'),
    ('clients.CompetitorGapReport', 'client', None,          'website_new'),
    ('clients.OnboardingToken',     'client', 'account_new', None),
    ('clients.OnboardingInvoice',   'client', 'account_new', 'website_new'),
    # reporting/
    ('reporting.GBPSyncCheck',      'client', None, 'website_new'),
    ('reporting.TrackedKeyword',    'client', None, 'website_new'),
    ('reporting.ConversionEvent',   'client', None, 'website_new'),
    ('reporting.MonthlyReport',     'client', None, 'website_new'),
    ('reporting.ContentFreshnessReport', 'client', None, 'website_new'),
    ('reporting.NPSSurvey',         'client', None, 'website_new'),
    ('reporting.BlogPost',          'client', None, 'website_new'),
    ('reporting.ClientChatbot',     'client', None, 'website_new'),
    ('reporting.VulnerabilityScan', 'client', None, 'website_new'),
    ('reporting.PageSession',       'client', None, 'website_new'),
    ('reporting.SessionRecording',  'client', None, 'website_new'),
    # vault/
    ('vault.ClientVault',           'client', 'account_new', None),
    ('vault.SSHSessionLog',         'client', 'account_new', 'website_new'),
    ('vault.OpsSession',            'client', 'account_new', 'website_new'),
    # domains/
    ('domains.DomainRegistration',  'client', 'account_new', 'pointed_at_website'),
    # billing/
    ('billing.MiniInvoice',         'client', 'account_new', 'website_new'),
    # sync/
    ('sync.SyncJob',                'client', 'account_new', 'website_new'),
    # admin_dashboard/
    ('admin_dashboard.DeploymentLog', 'client', 'account_new', 'website_new'),
]


def _repoint_dependents(client, account, website, *, dry_run, verbose):
    """
    Walk every model in DEPENDENT_REPOINTS and set account_new /
    website_new on rows that still FK this legacy client. Idempotent —
    only writes if the value would actually change.

    For website_new we use ``website`` (may be None for Account-only
    legacy profiles); rows there get their website_new left null,
    which is correct.
    """
    from django.apps import apps
    counts = Counter()

    for model_path, client_attr, account_attr, website_attr in (
            DEPENDENT_REPOINTS):
        model = apps.get_model(*model_path.split('.'))
        qs = model.objects.filter(**{client_attr: client})

        # Filter to rows that still need backfilling — avoids writes on
        # re-runs.
        update_kwargs = {}
        if account_attr is not None:
            update_kwargs[account_attr] = account
        if website_attr is not None and website is not None:
            update_kwargs[website_attr] = website

        if not update_kwargs:
            continue

        # Build a Q-style filter for "any of the target fields is still
        # null" so we don't churn already-backfilled rows.
        need_qs = qs
        if account_attr is not None:
            need_qs = need_qs.filter(**{f'{account_attr}__isnull': True})
        elif website_attr is not None and website is not None:
            need_qs = need_qs.filter(**{f'{website_attr}__isnull': True})

        n = need_qs.count()
        if n == 0:
            continue

        if verbose:
            print(f'    {model_path}: {n} row(s) → '
                  f'account={account.id if account_attr else "—"} '
                  f'website={website.id if (website and website_attr) else "—"}')

        if not dry_run:
            need_qs.update(**update_kwargs)
        counts[model_path] += n
    return counts


# ── Command ──────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        'Phase B backfill — populate Account / Website rows and repoint '
        'dependent FKs from legacy ClientProfile / Project. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print would-be mutations; write nothing.')
        parser.add_argument(
            '--verbose', action='store_true',
            help='Per-row detail (otherwise just per-client summary).')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']
        verbose = options['verbose']

        from clients.account_models import Account, Website
        from clients.models import ClientProfile

        clients = ClientProfile.objects.all().order_by('created_at')
        self.stdout.write(self.style.NOTICE(
            f'Found {clients.count()} legacy ClientProfile rows. '
            f'{"DRY RUN — " if dry_run else ""}Backfilling…'))
        self.stdout.write('')

        accounts_created = 0
        accounts_refreshed = 0
        websites_created = 0
        websites_refreshed = 0
        websites_skipped = 0
        total_repoints = Counter()

        # One transaction per legacy client. Failures roll back that
        # client only — others keep going so a single bad row doesn't
        # block the whole migration.
        for client in clients:
            try:
                with transaction.atomic():
                    self.stdout.write(
                        self.style.HTTP_INFO(
                            f'• {client.firm_name} ({client.pk})'))

                    # ── Account (always) ──
                    acc_defaults = _make_account_from_client(client)
                    if dry_run:
                        existing = Account.objects.filter(
                            legacy_client_profile=client).first()
                        if existing:
                            accounts_refreshed += 1
                            if verbose:
                                print(
                                    f'    Account exists → would refresh '
                                    f'{existing.id}')
                            account = existing
                        else:
                            accounts_created += 1
                            if verbose:
                                print(f'    Account → would CREATE')
                            account = None
                    else:
                        account, was_created = (
                            Account.objects.update_or_create(
                                legacy_client_profile=client,
                                defaults=acc_defaults,
                            ))
                        if was_created:
                            accounts_created += 1
                        else:
                            accounts_refreshed += 1

                    # ── Website (only if there's build data) ──
                    website = None
                    if not _client_has_website_data(client):
                        websites_skipped += 1
                        if verbose:
                            print(
                                f'    Website → skipped '
                                f'(no build data — Account-only)')
                    else:
                        project = _legacy_project_for(client)
                        if account is None:
                            # Dry-run path — account wasn't actually
                            # created. Skip website work, but still
                            # count it.
                            websites_created += 1
                            if verbose:
                                print(
                                    f'    Website → would CREATE '
                                    f'(account would be new)')
                        else:
                            ws_defaults = _make_website_from_client(
                                client, account, project)
                            # Idempotency key:
                            #   - prefer (account, legacy_project) when
                            #     a Project exists
                            #   - else (account, name) so a project-less
                            #     client doesn't get a duplicate row on
                            #     re-runs.
                            if project is not None:
                                lookup = {
                                    'account': account,
                                    'legacy_project': project,
                                }
                            else:
                                lookup = {
                                    'account': account,
                                    'name': client.firm_name,
                                    'legacy_project__isnull': True,
                                }
                            existing_ws = Website.objects.filter(
                                **lookup).first()

                            if existing_ws:
                                websites_refreshed += 1
                                if verbose:
                                    print(
                                        f'    Website exists → '
                                        f'refreshing {existing_ws.id}')
                                if not dry_run:
                                    for k, v in ws_defaults.items():
                                        setattr(existing_ws, k, v)
                                    existing_ws.save()
                                website = existing_ws
                            else:
                                websites_created += 1
                                if verbose:
                                    print(
                                        f'    Website → CREATING '
                                        f'(slug auto)')
                                if not dry_run:
                                    website = Website.objects.create(
                                        **ws_defaults)

                    # ── Dependent FK repoints ──
                    if account is not None:
                        counts = _repoint_dependents(
                            client, account, website,
                            dry_run=dry_run, verbose=verbose,
                        )
                        for k, v in counts.items():
                            total_repoints[k] += v

                    if dry_run and account is None:
                        # Account would be new — also need to roll
                        # back our atomic block to leave the DB clean.
                        raise _DryRunRollback()

            except _DryRunRollback:
                pass
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(
                    f'  ✗ FAILED for {client.firm_name} ({client.pk}) '
                    f'— {exc}'))
                continue

        # ── Summary ──
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            ('DRY RUN — would create / refresh:' if dry_run
             else 'DONE — created / refreshed:')))
        self.stdout.write(
            f'  Accounts   created: {accounts_created}  '
            f'refreshed: {accounts_refreshed}')
        self.stdout.write(
            f'  Websites   created: {websites_created}  '
            f'refreshed: {websites_refreshed}  '
            f'skipped: {websites_skipped}')
        if total_repoints:
            self.stdout.write('')
            self.stdout.write('  Dependent FKs repointed:')
            for model_path, n in sorted(total_repoints.items()):
                self.stdout.write(f'    {model_path}: {n}')
        else:
            self.stdout.write('  Dependent FKs: 0 rows needed updating.')

        if dry_run:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'No writes performed. Re-run without --dry-run to apply.'))


class _DryRunRollback(Exception):
    """Marker — raised inside an atomic block so dry-run leaves no trail."""
    pass
