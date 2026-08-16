"""
Repair the Account/Website parity findings that no backfill can decide.

``refactor_to_accounts`` and ``backfill_website_fks`` handle everything with
a mechanical answer: create the missing Account, adopt or create the Website,
and repoint dependent FKs whose owner is unambiguous.  Four classes of
finding are left over, and each one is a decision rather than a derivation:

``account-user-mismatch``
    An Account and its legacy ClientProfile disagree about which Django user
    owns them.  Whoever the ClientProfile points at is the login that has
    actually been used, so the Account is moved to it.  Skipped (and
    reported) when that user already holds a different Account, because the
    OneToOne cannot be satisfied without deciding which Account to discard —
    a merge, not a repair.

``website-legacy-project-account-mismatch``
    A Website carries a ``legacy_project`` belonging to some other Account,
    usually from an earlier hand-repair.  The link is cleared; the Project is
    then re-adopted by the correct Account on the next
    ``refactor_to_accounts`` run.

Duplicate external identifiers
    Two local rows claim the same Stripe customer, Stripe subscription, or
    DigitalOcean droplet.  Only one can be the real owner, and picking wrong
    routes a webhook — or a droplet destroy — at the wrong client.  Nothing
    is guessed: the manifest names the winner, and every other row has the
    identifier cleared.  ``--emit-manifest`` records the local ownership
    evidence for each candidate (payments collected, plan state, live site,
    droplet state) so the choice is made on facts rather than row age;
    ``--prefer-oldest`` exists for synthetic fixtures only and must never be
    used on production data.

    This command NEVER touches Stripe or DigitalOcean.  It clears a local
    column so two rows stop pointing at one remote object; no subscription
    is cancelled and no droplet is destroyed.  Reconciling the remote side
    is a separate, deliberate act.

Multi-website allocation
    Legacy client-level rows on an Account that owns more than one Website.
    The cutover contract forbids attaching them to the oldest Website, and
    ``backfill_website_fks`` therefore leaves them null unless the row's own
    ``project`` FK answers the question.  The residue is assigned here, per
    row, from the manifest — then the Account is stamped as reviewed.

Workflow::

    # 1. Write a template listing everything that needs a decision.
    python manage.py repair_account_website_parity --emit-manifest repair.json

    # 2. Edit repair.json — fill in every null.

    # 3. Dry run, then apply.
    python manage.py repair_account_website_parity --manifest repair.json
    python manage.py repair_account_website_parity --manifest repair.json --apply

Dry-run by default; ``--apply`` writes.
"""

import json
from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone


# (model label, field name) pairs the parity audit checks for duplicates.
DUPLICATE_CHECKS = [
    ('clients.Account', 'stripe_customer_id'),
    ('clients.Website', 'stripe_hosting_subscription_id'),
    ('clients.Website', 'stripe_maintenance_subscription_id'),
    ('clients.MaintenancePlan', 'stripe_subscription_id'),
    ('clients.SocialMediaPlan', 'stripe_subscription_id'),
    ('clients.Droplet', 'do_droplet_id'),
]

# Clearing a duplicate identifier from an Account or Website is not enough
# on its own. ClientProfile is still the live store during the transition:
# `backfill_account_data` copies its values forward, and the post_save sync
# in clients.signals does the same on every profile edit. Either one would
# restore the duplicate we just removed. So the legacy mirror is cleared in
# the same breath. Maps (canonical model label, canonical field) to the
# ClientProfile column that mirrors it.
LEGACY_MIRRORS = {
    ('clients.Account', 'stripe_customer_id'): 'stripe_customer_id',
    ('clients.Website', 'stripe_hosting_subscription_id'):
        'stripe_hosting_subscription_id',
    ('clients.Website', 'stripe_maintenance_subscription_id'):
        'stripe_subscription_id',
}


def _plain(value):
    """JSON-safe rendering of a field value for the manifest."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class Command(BaseCommand):
    help = ('Repair the parity findings that require an explicit decision: '
            'user mismatches, cross-account legacy links, duplicate external '
            'identifiers, and multi-website row allocation.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write changes (default: dry-run, no writes).')
        parser.add_argument(
            '--manifest', type=str, default='',
            help='JSON file naming identifier owners and multi-website row '
                 'assignments.')
        parser.add_argument(
            '--emit-manifest', type=str, default='',
            help='Write a manifest template for the current findings and '
                 'exit without changing anything.')
        parser.add_argument(
            '--prefer-oldest', action='store_true',
            help='SYNTHETIC FIXTURES ONLY. Keep a duplicated identifier on '
                 'the oldest row and clear the rest. Row age is not evidence '
                 'of ownership — never use this on production data; name the '
                 'winner in the manifest instead.')
        parser.add_argument(
            '--reviewed-by', type=str, default='',
            help='Name recorded on multi-website accounts as the reviewer.')

    # ── Entry point ─────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        self.apply = opts['apply']
        self.prefer_oldest = opts['prefer_oldest']
        self.reviewed_by = opts['reviewed_by']
        self.changes = 0
        self.blocked = []

        if opts['emit_manifest']:
            self._emit_manifest(Path(opts['emit_manifest']))
            return

        self.manifest = self._load_manifest(opts['manifest'])

        self.stdout.write('DRY RUN - no writes\n' if not self.apply
                          else 'APPLYING changes\n')
        with transaction.atomic():
            self._fix_user_mismatches()
            self._fix_cross_account_websites()
            self._fix_duplicate_identifiers()
            self._fix_orphan_projects()
            self._resolve_field_conflicts()
            self._apply_multi_website_mapping()
            if not self.apply:
                transaction.set_rollback(True)

        self.stdout.write('')
        self.stdout.write(
            f'Total repairs {"applied" if self.apply else "pending"}: '
            f'{self.changes}')
        if self.blocked:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Needs a human decision before the gate can pass:'))
            for line in self.blocked:
                self.stdout.write(f'  {line}')

    def _load_manifest(self, path):
        if not path:
            return {}
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != 1:
            raise CommandError(
                f'Unsupported manifest version: {data.get("version")!r} '
                '(expected 1).')
        return data

    def _note(self, message):
        self.stdout.write(f'  {message}')
        self.changes += 1

    # ── 1. Account/user mismatch ────────────────────────────────────────

    def _fix_user_mismatches(self):
        from clients.account_models import Account

        rows = Account.objects.filter(
            legacy_client_profile__isnull=False,
        ).exclude(
            user_id=F('legacy_client_profile__user_id'),
        ).select_related('legacy_client_profile__user', 'user')

        if not rows:
            return
        self.stdout.write('Account/user mismatches:')
        for account in rows:
            legacy_user = account.legacy_client_profile.user
            clash = Account.objects.filter(
                user=legacy_user).exclude(pk=account.pk).first()
            if clash is not None:
                self.blocked.append(
                    f'account-user-mismatch {account.pk}: user '
                    f'{legacy_user.email} already owns Account {clash.pk}. '
                    'Merge the two accounts by hand.')
                continue
            self._note(
                f'{account.name}: user '
                f'{account.user.email if account.user_id else "(none)"} -> '
                f'{legacy_user.email}')
            if self.apply:
                account.user = legacy_user
                account.save(update_fields=['user', 'updated_at'])

    # ── 2. Website linked to another account's Project ──────────────────

    def _fix_cross_account_websites(self):
        from clients.account_models import Website

        stale = []
        for website in Website.objects.filter(
                legacy_project__isnull=False).select_related(
                'legacy_project__client__migrated_account'):
            try:
                expected = website.legacy_project.client.migrated_account
            except Exception:
                continue
            if expected is not None and website.account_id != expected.pk:
                stale.append(website)

        if not stale:
            return
        self.stdout.write('Websites linked to another account\'s Project:')
        for website in stale:
            self._note(
                f'{website.name} ({website.pk}): clearing legacy_project '
                f'{website.legacy_project_id}')
            if self.apply:
                website.legacy_project = None
                website.save(update_fields=['legacy_project', 'updated_at'])

    # ── 3. Duplicate external identifiers ───────────────────────────────

    def _duplicate_groups(self):
        """Yield (model, field, value, rows) for every duplicated id."""
        for label, field in DUPLICATE_CHECKS:
            model = apps.get_model(*label.split('.'))
            values = (
                model.objects.exclude(**{field: ''})
                .values(field)
                .annotate(row_count=Count('pk'))
                .filter(row_count__gt=1)
                .order_by(field)
            )
            for row in values:
                value = row[field]
                rows = list(model.objects.filter(
                    **{field: value}).order_by('created_at'))
                yield label, model, field, value, rows

    def _fix_duplicate_identifiers(self):
        owners = (self.manifest.get('identifier_owners') or {})
        printed = False

        for label, model, field, value, rows in self._duplicate_groups():
            if not printed:
                self.stdout.write('Duplicate external identifiers:')
                printed = True

            key = f'{label}.{field}'
            winner_pk = (owners.get(key) or {}).get(value)
            winner = None
            if winner_pk:
                winner = next(
                    (r for r in rows if str(r.pk) == str(winner_pk)), None)
                if winner is None:
                    raise CommandError(
                        f'Manifest names {winner_pk} as the owner of '
                        f'{key}={value}, but no such row carries that value.')
            elif self.prefer_oldest:
                winner = rows[0]

            if winner is None:
                self.blocked.append(
                    f'{key}={value} is on {len(rows)} rows '
                    f'({", ".join(str(r.pk) for r in rows)}). Name the owner '
                    'in the manifest or pass --prefer-oldest.')
                continue

            mirror = LEGACY_MIRRORS.get((label, field))
            for row in rows:
                if row.pk == winner.pk:
                    continue
                self._note(
                    f'{key}={value}: clearing from {row.pk} '
                    f'(kept on {winner.pk})')
                if self.apply:
                    setattr(row, field, '')
                    row.save(update_fields=[field, 'updated_at'])
                if mirror:
                    self._clear_legacy_mirror(row, mirror, value)

    def _clear_legacy_mirror(self, row, mirror_field, value):
        """Blank the ClientProfile column that would restore a cleared id."""
        from clients.models import ClientProfile

        if hasattr(row, 'legacy_client_profile_id'):
            cp_id = row.legacy_client_profile_id
        elif hasattr(row, 'account'):
            cp_id = getattr(row.account, 'legacy_client_profile_id', None)
        else:
            cp_id = None
        if not cp_id:
            return
        # update() rather than save(): no post_save sync, no chance of the
        # value being written straight back onto the canonical row.
        updated = ClientProfile.objects.filter(
            pk=cp_id, **{mirror_field: value}).update(**{mirror_field: ''})
        if updated:
            self._note(
                f'  legacy ClientProfile {cp_id}.{mirror_field}: cleared '
                'to stop the backfill restoring it')

    # ── 3b. Legacy/canonical field conflicts ────────────────────────────

    def _field_conflicts(self):
        """Fields where legacy and canonical each hold a different real value.

        Not the same as a gap. A gap is missing data the backfill can carry
        across on its own; a conflict is two answers to one question, and
        during the transition neither store is automatically right — the
        portal writes Account and Website, while legacy paths still write
        ClientProfile. Guessing here reverses a payment status or a package.
        """
        from clients.account_models import Account
        from clients.parity import (
            ACCOUNT_FIELD_MAP, WEBSITE_FIELD_MAP, _classify)

        found = {}
        accounts = Account.objects.filter(
            legacy_client_profile__isnull=False,
        ).select_related('legacy_client_profile').prefetch_related('websites')

        for account in accounts:
            legacy = account.legacy_client_profile
            pairs = [('clients.Account', account, ACCOUNT_FIELD_MAP)]
            websites = list(account.websites.all())
            if len(websites) == 1:
                pairs.append(
                    ('clients.Website', websites[0], WEBSITE_FIELD_MAP))
            for label, row, field_map in pairs:
                for legacy_name, canonical_name in field_map.items():
                    legacy_value = getattr(legacy, legacy_name)
                    canonical_value = getattr(row, canonical_name)
                    if _classify(legacy_value, canonical_value) != 'conflict':
                        continue
                    found[f'{label}:{row.pk}:{canonical_name}'] = {
                        'canonical': _plain(canonical_value),
                        'legacy': _plain(legacy_value),
                        'legacy_field': legacy_name,
                        '_row': str(row),
                        'resolution': None,
                    }
        return found

    def _resolve_field_conflicts(self):
        from clients.models import ClientProfile

        conflicts = self._field_conflicts()
        if not conflicts:
            return
        declared = (self.manifest.get('field_conflicts') or {})
        self.stdout.write('Legacy/canonical field conflicts:')

        for key, info in conflicts.items():
            label, pk, canonical_name = key.split(':')
            choice = (declared.get(key) or {}).get('resolution')
            if choice not in ('canonical', 'legacy'):
                self.blocked.append(
                    f'{key}: canonical={info["canonical"]!r} '
                    f'legacy={info["legacy"]!r} — set '
                    f'field_conflicts["{key}"].resolution to "canonical" '
                    'or "legacy".')
                continue

            model = apps.get_model(*label.split('.'))
            row = model.objects.get(pk=pk)
            legacy_name = info['legacy_field']
            profile_id = (row.legacy_client_profile_id
                          if label == 'clients.Account'
                          else row.account.legacy_client_profile_id)

            if choice == 'canonical':
                # Canonical wins: copy it onto the legacy row so the two
                # agree and nothing is lost when the legacy table is dropped.
                value = getattr(row, canonical_name)
                self._note(f'{key}: legacy <- canonical {value!r}')
                if self.apply:
                    ClientProfile.objects.filter(pk=profile_id).update(
                        **{legacy_name: value})
            else:
                value = getattr(
                    ClientProfile.objects.get(pk=profile_id), legacy_name)
                self._note(f'{key}: canonical <- legacy {value!r}')
                if self.apply:
                    model.objects.filter(pk=pk).update(
                        **{canonical_name: value})

    # ── 4. Orphaned legacy Projects ─────────────────────────────────────

    def _orphan_projects(self):
        from clients.models import Project

        return list(
            Project.objects.filter(migrated_website__isnull=True)
            .select_related('client__migrated_account')
            .order_by('created_at'))

    def _fix_orphan_projects(self):
        """Give every leftover Project a canonical Website.

        ``refactor_to_accounts`` builds one Website per ClientProfile, from
        that client's live (or newest) Project. A client who started three
        builds therefore leaves the other two unmapped, and the cutover
        contract requires every Project to be explicitly accounted for
        before the table can be dropped.

        The manifest names the action per project. ``archive_as_website``
        materialises an archived Website carrying that build's own stage and
        payment state — the history is preserved, the invariant holds, and
        the site never shows up as an active build. Nothing is deleted here;
        discarding a Project is a decision to take deliberately, not a side
        effect of a repair run.
        """
        from clients.account_models import Website

        orphans = self._orphan_projects()
        if not orphans:
            return

        actions = (self.manifest.get('orphan_projects') or {})
        self.stdout.write('Legacy Projects with no Website:')
        for project in orphans:
            entry = actions.get(str(project.pk)) or {}
            action = entry.get('action')
            account = getattr(project.client, 'migrated_account', None)

            if account is None:
                self.blocked.append(
                    f'project {project.pk} has no Account — run '
                    'refactor_to_accounts first.')
                continue
            if action != 'archive_as_website':
                self.blocked.append(
                    f'project {project.pk} ({project.client.firm_name}, '
                    f'stage={project.stage}) has no Website. Set '
                    f'orphan_projects["{project.pk}"].action to '
                    '"archive_as_website" in the manifest.')
                continue

            name = (entry.get('name')
                    or f'{project.client.firm_name} (archived build)')
            self._note(
                f'{project.pk}: archiving as Website "{name}"')
            if self.apply:
                Website.objects.create(
                    account=account,
                    name=name,
                    business_type=project.client.business_type or '',
                    url=project.live_url or '',
                    staging_url=project.staging_url or '',
                    status='archived',
                    stage=project.stage or 'intake',
                    package=project.package or '',
                    onboarding_status='pending_intake',
                    payment_status=project.payment_status
                    or 'awaiting_deposit',
                    deposit_paid_at=project.deposit_paid_at,
                    final_paid_at=project.final_paid_at,
                    revision_count=project.revision_count or 0,
                    revision_limit=project.revision_limit or 2,
                    launch_date=project.launch_date,
                    support_window_ends=project.support_window_ends,
                    moonieful_referred=bool(project.moonieful_referred),
                    moonieful_handoff_at=project.moonieful_handoff_at,
                    moonieful_stage_history=(
                        project.moonieful_stage_history or []),
                    legacy_project=project,
                )

    # ── 5. Multi-website row allocation ─────────────────────────────────

    def _ambiguous_rows(self):
        """Legacy rows on multi-website accounts with no canonical website.

        Only rows whose owning Account holds more than one Website and whose
        website FK is still null — everything else was already resolved
        mechanically.
        """
        from clients.account_models import Account, Website

        multi = {
            a.pk: a for a in Account.objects.annotate(
                site_count=Count('websites')).filter(site_count__gt=1)
        }
        if not multi:
            return {}, {}

        cp_to_account = {
            a.legacy_client_profile_id: a
            for a in Account.objects.filter(
                legacy_client_profile__isnull=False)
            if a.pk in multi
        }

        found = {}
        for model in apps.get_models():
            names = {f.name: f for f in model._meta.get_fields()}
            client_field = names.get('client')
            if (client_field is None or getattr(
                    client_field, 'related_model', None) is None
                    or client_field.related_model.__name__
                    != 'ClientProfile'):
                continue
            site_field = None
            for candidate in ('website_new', 'website',
                              'pointed_at_website'):
                field = names.get(candidate)
                if (field is not None
                        and getattr(field, 'related_model', None)
                        is Website):
                    site_field = candidate
                    break
            if site_field is None:
                continue

            label = model._meta.label
            for row in model.objects.filter(
                    client__isnull=False,
                    **{f'{site_field}__isnull': True}).iterator():
                account = cp_to_account.get(row.client_id)
                if account is None:
                    continue
                found.setdefault(str(account.pk), {}).setdefault(
                    label, {})[str(row.pk)] = site_field
        return found, multi

    def _evidence_for(self, row):
        """Local ownership evidence for one side of a duplicate identifier.

        A duplicated Stripe or DigitalOcean ID means two local rows claim
        one remote object, and clearing it from the wrong row sends future
        webhooks — or a droplet destroy — to the wrong client. "Oldest
        wins" is a coin flip dressed as a rule; row age says nothing about
        which client is actually being billed.

        So the manifest carries what the local database knows about each
        candidate: money actually collected, plan state, live site, droplet
        state. Cross-check against the Stripe customer's own invoice
        history and the DO droplet's name before choosing.
        """
        info = {'pk': str(row.pk), 'label': str(row),
                'created_at': row.created_at.isoformat()}
        model = row._meta.label

        account = None
        if model == 'clients.Account':
            account = row
            info['websites'] = [w.name for w in row.websites.all()]
            info['legacy_firm_name'] = (
                row.legacy_client_profile.firm_name
                if row.legacy_client_profile_id else None)
        elif hasattr(row, 'account_id') and row.account_id:
            account = row.account
            info['account'] = account.name
        if model == 'clients.Website':
            info['url'] = row.url or row.staging_url or ''
            info['stage'] = row.stage
            info['account'] = row.account.name

        if model in ('clients.MaintenancePlan', 'clients.SocialMediaPlan'):
            info['tier'] = row.tier_slug
            info['status'] = row.status
            info['started_at'] = (
                row.started_at.isoformat() if row.started_at else None)
            info['site'] = (row.website.name if row.website_id
                            else row.external_site_url or None)
        if model == 'clients.Droplet':
            info['status'] = row.status
            info['ip'] = row.do_droplet_ip
            info['provisioned_at'] = (
                row.provisioned_at.isoformat() if row.provisioned_at
                else None)
            info['site'] = row.website.name if row.website_id else None

        # Money is the strongest local signal of who the remote object
        # actually belongs to.
        if account is not None:
            payments = account.payment_records.order_by('-paid_at')
            info['payments_recorded'] = payments.count()
            latest = payments.first()
            info['latest_payment'] = (
                f'{latest.amount} {latest.kind} '
                f'{latest.paid_at:%Y-%m-%d}' if latest and latest.paid_at
                else None)
            info['active_plans'] = (
                account.maintenance_plans.filter(status='active').count()
                + account.social_media_plans.filter(status='active').count())
        return info

    def _emit_manifest(self, path):
        from clients.account_models import Account

        manifest = {
            'version': 1,
            'note': ('Fill in every null before applying. Values are primary '
                     'keys.'),
            'identifier_owners': {},
            'field_conflicts': self._field_conflicts(),
            'orphan_projects': {},
            'multi_website_accounts': {},
        }

        for project in self._orphan_projects():
            manifest['orphan_projects'][str(project.pk)] = {
                'action': None,
                'name': None,
                '_client': project.client.firm_name,
                '_stage': project.stage,
                '_payment_status': project.payment_status,
                '_created_at': project.created_at.isoformat(),
            }

        for label, model, field, value, rows in self._duplicate_groups():
            key = f'{label}.{field}'
            manifest['identifier_owners'].setdefault(key, {})[value] = None
            manifest.setdefault('_duplicate_candidates', {}).setdefault(
                key, {})[value] = [self._evidence_for(r) for r in rows]

        ambiguous, multi = self._ambiguous_rows()
        for account_pk, account in multi.items():
            entry = {
                'note': '',
                'reviewed_by': '',
                'websites': [
                    {'pk': str(w.pk), 'name': w.name, 'slug': w.slug,
                     'stage': w.stage}
                    for w in account.websites.order_by('created_at')
                ],
                'rows': {
                    label: {pk: None for pk in rows}
                    for label, rows in ambiguous.get(
                        str(account_pk), {}).items()
                },
            }
            manifest['multi_website_accounts'][str(account_pk)] = entry

        path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        pending = sum(
            len(rows)
            for entry in manifest['multi_website_accounts'].values()
            for rows in entry['rows'].values())
        self.stdout.write(self.style.SUCCESS(f'Wrote {path}'))
        self.stdout.write(
            f'  duplicate identifier groups: '
            f'{sum(len(v) for v in manifest["identifier_owners"].values())}')
        self.stdout.write(
            f'  orphan projects: {len(manifest["orphan_projects"])}')
        self.stdout.write(
            f'  multi-website accounts: '
            f'{len(manifest["multi_website_accounts"])}')
        self.stdout.write(f'  rows awaiting an explicit website: {pending}')

    def _apply_multi_website_mapping(self):
        from clients.account_models import Account, Website

        mapping = (self.manifest.get('multi_website_accounts') or {})
        ambiguous, multi = self._ambiguous_rows()
        if not multi:
            return

        self.stdout.write('Multi-website accounts:')
        for account_pk, account in multi.items():
            entry = mapping.get(str(account_pk))
            rows_left = ambiguous.get(str(account_pk), {})

            if entry is None:
                self.blocked.append(
                    f'multi-website account {account_pk} ({account.name}) has '
                    'no manifest entry. Emit one with --emit-manifest.')
                continue

            assignments = entry.get('rows') or {}
            unresolved = 0
            for label, rows in rows_left.items():
                declared = assignments.get(label) or {}
                model = apps.get_model(*label.split('.'))
                for row_pk, site_field in rows.items():
                    target = declared.get(row_pk)
                    if not target:
                        unresolved += 1
                        self.blocked.append(
                            f'{label} {row_pk} on account {account_pk} has no '
                            'website assigned in the manifest.')
                        continue
                    website = Website.objects.filter(pk=target).first()
                    if website is None or website.account_id != account.pk:
                        raise CommandError(
                            f'Manifest assigns {label} {row_pk} to website '
                            f'{target}, which does not belong to account '
                            f'{account_pk}.')
                    self._note(
                        f'{label} {row_pk} -> {website.name}')
                    if self.apply:
                        model.objects.filter(pk=row_pk).update(
                            **{site_field: website})

            if unresolved:
                continue

            # Already reviewed, and no Website has appeared since — leave the
            # timestamp alone so a repeat run is a genuine no-op.
            reviewed = account.multi_website_reviewed_at
            if reviewed is not None and not account.websites.filter(
                    created_at__gt=reviewed).exists():
                continue

            # Everything allocated — record the review.  A Website created
            # after this timestamp re-opens the warning, which is the point:
            # a new site means the mapping has not considered it.
            note = entry.get('note') or ''
            reviewer = (entry.get('reviewed_by') or self.reviewed_by
                        or 'unspecified')
            stamp = (f'{note} (reviewed by {reviewer})' if note
                     else f'Reviewed by {reviewer}')
            self._note(
                f'{account.name}: recording multi-website review')
            if self.apply:
                Account.objects.filter(pk=account.pk).update(
                    multi_website_reviewed_at=timezone.now(),
                    multi_website_review_note=stamp)
