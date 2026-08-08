"""
Backfill `website_new` / `account_new` FKs from the legacy `client`
(ClientProfile) FK across every model that still carries both.

Part of the Phase-D ClientProfile teardown: once every dependent row
points at an Account/Website, the legacy `client`/`project` columns can
be dropped without orphaning data.

Mapping rule:
  - account_new  = client.migrated_account
  - website_new  = that account's PRIMARY website (oldest by created_at)

Multi-website accounts: historical client-level rows can't be split per
site, so they attach to the primary website. Acceptable — this data is
disposable (pre-launch clients) and going forward collection is
per-website.

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
        from clients.account_models import Account

        # account by legacy ClientProfile id
        acct_by_cp = {
            a.legacy_client_profile_id: a
            for a in Account.objects.filter(
                legacy_client_profile__isnull=False)
        }
        # primary (oldest) website per account
        primary_site = {}
        for a in Account.objects.all():
            w = a.websites.order_by('created_at').first()
            if w:
                primary_site[a.id] = w

        self.stdout.write(
            f'Accounts: {Account.objects.count()} | '
            f'CP->account map: {len(acct_by_cp)} | '
            f'accounts with a website: {len(primary_site)}')
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

            label = f'{model._meta.app_label}.{model.__name__}'
            if via_vault:
                qs = (model.objects
                      .filter(vault__client__isnull=False)
                      .select_related('vault'))
            else:
                qs = model.objects.filter(client__isnull=False)
            fixed_w = fixed_a = skipped = 0
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
                    site = primary_site.get(acct.id)
                    if site is not None:
                        setattr(row, site_field, site)
                        changed.append(site_field)
                        fixed_w += 1
                if changed and apply:
                    row.save(update_fields=changed)
            if fixed_w or fixed_a or skipped:
                total += fixed_w + fixed_a
                self.stdout.write(
                    f'  {label}: website_new+={fixed_w} '
                    f'account_new+={fixed_a} skipped(no account)={skipped}')

        self.stdout.write(
            f'\nTotal field writes {"applied" if apply else "pending"}: '
            f'{total}')
