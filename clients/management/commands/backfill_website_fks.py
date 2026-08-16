"""
Backfill `website_new` / `account_new` FKs from the legacy `client`
(ClientProfile) FK across every model that still carries both.

Part of the Phase-D ClientProfile teardown: once every dependent row
points at an Account/Website, the legacy `client`/`project` columns can
be dropped without orphaning data.

Mapping rule for ``website_new``, in order:

  1. The row's own legacy ``project`` FK, resolved through
     ``Project.migrated_website``. This is an exact answer, not a guess —
     use it whenever the row has one.
  2. The account's only Website, when it has exactly one.
  3. Nothing. The row is left null and counted as ``ambiguous``.

``account_new`` is always ``client.migrated_account``.

Rule 3 is the important one. This command used to attach every unresolved
row to the account's oldest Website, which quietly mis-files data on any
account that owns more than one site — a Vance Mediation support ticket
landing under Vance Family Law, permanently and invisibly. The cutover
contract forbids that ("never silently attach legacy website-level rows to
the oldest website when an account has more than one"). Ambiguous rows now
stay null so the parity audit keeps reporting them, and an operator resolves
them with an explicit mapping via ``repair_account_website_parity``.

Idempotent + dry-run by default. Run with --apply to write.
"""

from django.apps import apps
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Backfill website_new/account_new from the legacy client FK."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write changes (default: dry-run, no writes).')

    def handle(self, *args, **opts):
        apply = opts['apply']
        from clients.account_models import Account, Website

        # account by legacy ClientProfile id
        acct_by_cp = {
            a.legacy_client_profile_id: a
            for a in Account.objects.filter(
                legacy_client_profile__isnull=False)
        }
        # The unambiguous website per account: only accounts that own
        # exactly one. Multi-website accounts are deliberately absent —
        # their rows must be resolved by project FK or by hand.
        sole_site = {}
        multi_site_accounts = set()
        for a in Account.objects.all():
            sites = list(a.websites.order_by('created_at')[:2])
            if len(sites) == 1:
                sole_site[a.id] = sites[0]
            elif len(sites) > 1:
                multi_site_accounts.add(a.id)

        # legacy Project id -> canonical Website, for rule 1.
        site_by_project = {
            w.legacy_project_id: w
            for w in Website.objects.filter(legacy_project__isnull=False)
        }

        self.stdout.write(
            f'Accounts: {Account.objects.count()} | '
            f'CP->account map: {len(acct_by_cp)} | '
            f'single-website accounts: {len(sole_site)} | '
            f'multi-website accounts: {len(multi_site_accounts)}')
        self.stdout.write('DRY RUN - no writes\n' if not apply
                          else 'APPLYING changes\n')

        total = 0
        for model in apps.get_models():
            field_names = {f.name for f in model._meta.get_fields()}
            # Must have a `client` FK pointing at ClientProfile.
            client_field = next(
                (f for f in model._meta.get_fields()
                 if f.name == 'client' and getattr(f, 'related_model', None)
                 and f.related_model.__name__ == 'ClientProfile'), None)
            # Vault models reach ClientProfile one hop out, through
            # ClientVault (`vault.client`) rather than a direct `client`
            # FK — VaultCredential is the one that matters. Resolve the
            # owning ClientProfile per row instead of skipping the model.
            via_vault = False
            if client_field is None:
                vault_field = next(
                    (f for f in model._meta.get_fields()
                     if f.name == 'vault'
                     and getattr(f, 'related_model', None)
                     and f.related_model.__name__ == 'ClientVault'), None)
                if vault_field is None:
                    continue
                via_vault = True
            # Account FK may be named account_new (legacy-coexist models) or
            # account (newer models like PaymentRecord/plans). Same for
            # website_new vs website. Verify each points at the right model.
            def _fk(name, target):
                f = next((g for g in model._meta.get_fields()
                          if g.name == name and getattr(g, 'related_model', None)
                          and g.related_model.__name__ == target), None)
                return name if f is not None else None

            acct_field = _fk('account_new', 'Account') or _fk('account', 'Account')
            site_field = _fk('website_new', 'Website') or _fk('website', 'Website')
            if not (acct_field or site_field):
                continue

            # A legacy `project` FK on the row is the exact answer for
            # website_new — better than any account-level inference.
            project_field = _fk('project', 'Project')

            label = f'{model._meta.app_label}.{model.__name__}'
            if via_vault:
                qs = (model.objects
                      .filter(vault__client__isnull=False)
                      .select_related('vault'))
            else:
                qs = model.objects.filter(client__isnull=False)
            fixed_w = fixed_a = skipped = ambiguous = 0
            for row in qs.iterator():
                cp_id = (row.vault.client_id if via_vault
                         else row.client_id)
                acct = acct_by_cp.get(cp_id)
                if acct is None:
                    skipped += 1
                    continue
                changed = []
                if acct_field and getattr(row, acct_field + '_id', None) is None:
                    setattr(row, acct_field, acct)
                    changed.append(acct_field)
                    fixed_a += 1
                if site_field and getattr(row, site_field + '_id', None) is None:
                    site = None
                    if project_field:
                        site = site_by_project.get(
                            getattr(row, project_field + '_id', None))
                    if site is None:
                        site = sole_site.get(acct.id)
                    if site is not None:
                        setattr(row, site_field, site)
                        changed.append(site_field)
                        fixed_w += 1
                    else:
                        # Multi-website account and no project FK to
                        # disambiguate. Leave it null on purpose.
                        ambiguous += 1
                if changed and apply:
                    row.save(update_fields=changed)
            if fixed_w or fixed_a or skipped or ambiguous:
                total += fixed_w + fixed_a
                line = (f'  {label}: website_new+={fixed_w} '
                        f'account_new+={fixed_a} '
                        f'skipped(no account)={skipped}')
                if ambiguous:
                    line += f' AMBIGUOUS(needs manual mapping)={ambiguous}'
                self.stdout.write(line)

        self.stdout.write(
            f'\nTotal field writes {"applied" if apply else "pending"}: '
            f'{total}')
