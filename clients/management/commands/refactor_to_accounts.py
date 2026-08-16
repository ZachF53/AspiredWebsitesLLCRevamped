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

def _suppress_autocreate_signal(client):
    """Mark a ClientProfile so the post_save Account/Website
    autocreate signal short-circuits — this command does its own
    creation and we don't want a duplicate fire."""
    client._skip_autocreate = True


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


# Package codes that mean "we are building (or built) a site for them".
# The maintenance_* and moonieful_referred codes are service entitlements,
# not builds — see _client_has_website_data.
_BUILD_PACKAGES = {'essential_build', 'premium_build'}


def _client_has_website_data(client):
    """
    True if this legacy ClientProfile carries enough build state to
    justify creating a Website. Auxiliary vault-only profiles (no
    project, no URL, no droplet, default stage) return False — they
    become Account-only.

    Subscription-only buyers are Account-only too.  Someone who bought
    maintenance or social for a site we neither built nor host has no build
    to represent: their entitlement is a MaintenancePlan / SocialMediaPlan
    row carrying ``external_site_url``.  ``clients.signals`` already refuses
    to autocreate a Website for them (``_skip_website_autocreate``) because a
    build Website would surface build-only portal nav — My Project, Intake,
    Revisions — that they can never complete.  This command has to apply the
    same rule or it re-creates exactly the row the signal declined to make.
    That is why ``maintenance_active`` is not evidence of a build, and why
    only the two build packages count.
    """
    if client.projects.exists():
        return True
    if (client.website or '').strip():
        return True
    if (getattr(client, 'do_droplet_id', '') or '').strip():
        return True
    if (client.package or '').strip() in _BUILD_PACKAGES:
        return True
    if client.launch_date:
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
        # Copied verbatim, NOT coerced to b''. `backfill_account_data`
        # copies the profile's value across as-is, so coercing None to b''
        # here made the two commands disagree forever: this one wrote b'',
        # that one wrote None back, and every rehearsal pass reported nine
        # phantom changes. A BinaryField with null=True has None as its
        # empty value; b'' is a different value.
        'client_pin_salt': client.client_pin_salt,
        'client_pin_set': bool(client.client_pin_set),
        'client_pin_failed_attempts': (
            client.client_pin_failed_attempts or 0),
        'client_pin_lockout_until': client.client_pin_lockout_until,
        'moonieful_client_id': client.moonieful_client_id,
        'synced_from_moonieful': bool(client.synced_from_moonieful),
        'last_synced_at': client.last_synced_at,
        'sync_conflict_flagged': bool(client.sync_conflict_flagged),
    }


def _apply_changed(instance, values):
    """
    Assign only the fields whose value actually differs and save those.

    Blanket ``update_or_create`` / ``save()`` rewrites every column on every
    run, which bumps ``updated_at`` (``auto_now``) even when nothing changed.
    That is not cosmetic here: the Moonieful bridge decides whether an
    inbound record is stale by comparing ``updated_at``, so a backfill that
    touches every row makes every local record look newer than Miki's and
    suppresses legitimate inbound updates. It also means a re-run can never
    be proven to be a no-op.

    Returns the list of field names written (empty when nothing changed).
    """
    changed = []
    for name, value in values.items():
        field = instance._meta.get_field(name)
        if field.is_relation:
            current = getattr(instance, f'{name}_id')
            incoming = value.pk if value is not None else None
        else:
            current = getattr(instance, name)
            incoming = value
        if current != incoming:
            changed.append(name)
    if not changed:
        return []
    for name in changed:
        setattr(instance, name, values[name])
    instance.save(update_fields=changed + ['updated_at'])
    return changed


def _fill_missing(instance, values):
    """Populate only the fields the canonical row has not got yet.

    A backfill fills gaps. It must not overwrite a populated canonical value
    with a legacy one, because ClientProfile stopped being the live store
    the moment Websites became editable in the Account/Website admin.

    The real-data rehearsal proved the cost of getting this wrong. Blanket
    refresh from the legacy profile did this to a live, paying client:

        url                  'whiteheadwellness.com' -> ''
        package              'premium_build'         -> ''
        payment_status       'fully_paid'            -> 'awaiting_deposit'
        maintenance_active   True                    -> False
        business_type        'Health and Wellness'   -> ''
        do_droplet_name      'whitehead-wellness-prod' -> ''

    — a launched site reverted to awaiting-deposit with its maintenance
    subscription flag off and its URL erased. ``do_droplet_name`` was wiped
    on nine sites outright, because the legacy profile has no such column
    and the mapping passes ``''``.

    "Not got yet" means None or empty string only. False and 0 are real
    values a person chose; treating them as empty would flip
    ``session_recording_enabled`` and reset ``revision_count`` from stale
    legacy rows. Where both sides hold a real value and they disagree, that
    is a conflict for a human, and the parity audit reports it as one.
    """
    changed = []
    for name, value in values.items():
        if value is None or value == '':
            continue  # nothing to contribute
        field = instance._meta.get_field(name)
        current = (getattr(instance, f'{name}_id') if field.is_relation
                   else getattr(instance, name))
        if current is None or current == '':
            setattr(instance, name, value)
            changed.append(name)
    if changed:
        instance.save(update_fields=changed + ['updated_at'])
    return changed


def _existing_website_for(account, client, project):
    """
    Find the Website this legacy client's build already lives on, or None.

    Ordinary idempotency (``account`` + ``legacy_project``) is not enough on
    its own.  Since Phase C, ``clients.signals.autocreate_account_and_website``
    materialises a Website as soon as the ClientProfile is created, and it
    leaves ``legacy_project`` NULL because it never looks at Project rows.
    Keying only on ``legacy_project`` misses that row, so this command used to
    create a SECOND Website for every client the signal had already handled —
    doubling the table and pushing the new row onto a ``-2`` slug.  The
    rehearsal reproduced exactly that: 8 websites became 16.

    Resolution order:
      1. The Website already linked to this Project (a true re-run).
      2. An unlinked Website on this Account with the same business name —
         the signal-created row.  Adopt it.
      3. This Account's only Website, if it is unlinked — same case, but the
         firm name was edited after the signal fired.
    Anything else returns None and a new Website is created.  Rules 2 and 3
    both require ``legacy_project`` to be NULL, so a Website already claimed
    by another Project is never stolen.
    """
    from clients.account_models import Website

    if project is not None:
        linked = Website.objects.filter(
            account=account, legacy_project=project).first()
        if linked is not None:
            return linked

    unlinked = Website.objects.filter(
        account=account, legacy_project__isnull=True)

    by_name = unlinked.filter(name=client.firm_name).first()
    if by_name is not None:
        return by_name

    if account.websites.count() == 1:
        return unlinked.first()
    return None


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

    The account side is unambiguous: one legacy ClientProfile maps to one
    Account, so those rows are updated in bulk.

    The website side is not. This used to bulk-assign ``website`` — the one
    Website this command had just built for the client — to every dependent
    row. On an Account owning several sites that silently mis-files data:
    the rehearsal caught a "Mediation site: intake form not emailing" ticket
    landing under Vance Family Law, and nothing in the parity audit can spot
    it afterwards, because a wrong-but-populated FK looks exactly like a
    right one. Website assignment is therefore resolved per row:

      1. the row's own legacy ``project`` FK, mapped through the Website
         that adopted that Project;
      2. the account's only Website, when it owns exactly one;
      3. otherwise nothing — left null, counted as ambiguous, and resolved
         by hand via ``repair_account_website_parity``.

    Returns ``(written, ambiguous)`` counters.
    """
    from django.apps import apps
    counts = Counter()
    ambiguous = Counter()

    account_sites = list(account.websites.all())
    sole_site = account_sites[0] if len(account_sites) == 1 else None
    site_by_project = {
        site.legacy_project_id: site
        for site in account_sites if site.legacy_project_id
    }

    for model_path, client_attr, account_attr, website_attr in (
            DEPENDENT_REPOINTS):
        model = apps.get_model(*model_path.split('.'))
        qs = model.objects.filter(**{client_attr: client})

        if account_attr is not None:
            need_qs = qs.filter(**{f'{account_attr}__isnull': True})
            n = need_qs.count()
            if n:
                if verbose:
                    print(f'    {model_path}: {n} row(s) → '
                          f'account={account.id}')
                if not dry_run:
                    need_qs.update(**{account_attr: account})
                counts[model_path] += n

        if website_attr is None:
            continue

        has_project = any(
            f.name == 'project' for f in model._meta.get_fields())
        for row in qs.filter(
                **{f'{website_attr}__isnull': True}).iterator():
            target = None
            if has_project and getattr(row, 'project_id', None):
                target = site_by_project.get(row.project_id)
            if target is None:
                target = sole_site
            if target is None:
                ambiguous[model_path] += 1
                continue
            if verbose:
                print(f'    {model_path} {row.pk} → website={target.id}')
            if not dry_run:
                model.objects.filter(pk=row.pk).update(
                    **{website_attr: target})
            counts[model_path] += 1

    return counts, ambiguous


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
        total_ambiguous = Counter()

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
                        account = Account.objects.filter(
                            legacy_client_profile=client).first()
                        if account is None:
                            account = Account.objects.create(
                                legacy_client_profile=client, **acc_defaults)
                            accounts_created += 1
                        elif _fill_missing(account, acc_defaults):
                            # Same rule as Websites: the portal settings page
                            # writes Account directly, so a populated Account
                            # field is not stale data to be corrected from the
                            # legacy profile.
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
                            existing_ws = _existing_website_for(
                                account, client, project)

                            if existing_ws:
                                # Fill gaps only — never clobber a live
                                # Website with stale legacy values.
                                if dry_run:
                                    websites_refreshed += 1
                                elif _fill_missing(existing_ws, ws_defaults):
                                    websites_refreshed += 1
                                    if verbose:
                                        print(
                                            f'    Website {existing_ws.id} → '
                                            f'gaps filled')
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
                        counts, ambiguous = _repoint_dependents(
                            client, account, website,
                            dry_run=dry_run, verbose=verbose,
                        )
                        for k, v in counts.items():
                            total_repoints[k] += v
                        for k, v in ambiguous.items():
                            total_ambiguous[k] += v

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

        if total_ambiguous:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                '  Left null — multi-website account, no project FK to '
                'resolve them. Map with repair_account_website_parity:'))
            for model_path, n in sorted(total_ambiguous.items()):
                self.stdout.write(f'    {model_path}: {n}')

        if dry_run:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'No writes performed. Re-run without --dry-run to apply.'))


class _DryRunRollback(Exception):
    """Marker — raised inside an atomic block so dry-run leaves no trail."""
    pass
