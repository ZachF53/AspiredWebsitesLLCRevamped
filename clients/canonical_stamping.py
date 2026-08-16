"""
Stamp canonical Account/Website FKs at write time.

Every dependent model carries both a legacy owner (`client`, sometimes
`project`) and a canonical one (`account_new`/`account`,
`website_new`/`website`). Code that creates such a row is supposed to set
both. In practice it sets the legacy FK and forgets the canonical one,
because the legacy FK is the one the surrounding code has in hand.

The consequence is silent: the row saves, the write looks successful, and
the record is simply invisible to anything reading canonically — an admin
list, a portal page, a report scoped by Website. Nothing errors. It has
recurred repeatedly, and a backfill only repairs rows that already exist;
the next unstamped writer recreates the problem the following day.

So the guarantee moves to the write itself. Any row saved with a legacy
owner and a null canonical owner gets the canonical FK derived and filled
in before it hits the database.

Derivation is deliberately identical to `backfill_website_fks`, because
two rules that are supposed to agree will not stay in agreement:

  account  = client.migrated_account
  website  = the row's own project -> Website, when it has a project FK
             else the account's only Website, when it owns exactly one
             else NOTHING — left null on purpose

That last branch matters. On an account owning several Websites, with no
project FK to disambiguate, there is no correct answer, and picking the
oldest is exactly the silent mis-filing the cutover contract forbids. The
row stays null, the parity audit keeps reporting it, and a human maps it.

This never overwrites a canonical FK that is already set, and it never
runs during fixture loads.
"""

import logging

from django.db.models.signals import pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Populated by install(); maps model -> (account_field, website_field,
# has_project). Built once at startup so the per-save path does no
# introspection.
_STAMP_PLAN = {}


def _resolve_field(model, names, target_name):
    for name in names:
        try:
            field = model._meta.get_field(name)
        except Exception:
            continue
        related = getattr(field, 'related_model', None)
        if related is not None and related.__name__ == target_name:
            return name
    return None


def build_plan():
    """Work out which models need stamping and on which fields."""
    from django.apps import apps

    plan = {}
    for model in apps.get_models():
        names = {f.name for f in model._meta.get_fields()}
        client_field = _resolve_field(model, ('client',), 'ClientProfile')
        if client_field is None:
            continue
        account_field = _resolve_field(
            model, ('account_new', 'account'), 'Account')
        website_field = _resolve_field(
            model, ('website_new', 'website', 'pointed_at_website'),
            'Website')
        if not (account_field or website_field):
            continue
        project_field = _resolve_field(model, ('project',), 'Project')
        plan[model] = (account_field, website_field, project_field)
    return plan


def stamp(instance):
    """Fill in whichever canonical FKs are missing. Returns fields set."""
    entry = _STAMP_PLAN.get(type(instance))
    if entry is None:
        return []
    account_field, website_field, project_field = entry

    needs_account = (
        account_field
        and getattr(instance, f'{account_field}_id', None) is None)
    needs_website = (
        website_field
        and getattr(instance, f'{website_field}_id', None) is None)
    if not (needs_account or needs_website):
        return []

    client_id = getattr(instance, 'client_id', None)
    if client_id is None:
        return []

    try:
        client = instance.client
        account = getattr(client, 'migrated_account', None)
    except Exception:
        return []
    if account is None:
        return []

    changed = []
    if needs_account:
        setattr(instance, account_field, account)
        changed.append(account_field)

    if needs_website:
        website = None
        if project_field:
            project = getattr(instance, project_field, None)
            if project is not None:
                website = getattr(project, 'migrated_website', None)
        if website is None:
            sites = list(account.websites.all()[:2])
            if len(sites) == 1:
                website = sites[0]
        if website is not None:
            setattr(instance, website_field, website)
            changed.append(website_field)
        # else: multi-website account with nothing to disambiguate on.
        # Left null deliberately — see module docstring.

    return changed


@receiver(pre_save)
def stamp_canonical_owner(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return  # fixture load — the fixture carries its own FKs
    if sender not in _STAMP_PLAN:
        return
    try:
        changed = stamp(instance)
    except Exception:
        # A stamping failure must never block the write it decorates.
        logger.exception(
            'canonical stamping failed for %s', sender._meta.label)
        return
    if changed:
        logger.debug(
            'canonical stamping set %s on %s',
            ', '.join(changed), sender._meta.label)


def install():
    """Build the plan. Call from AppConfig.ready() after models load."""
    _STAMP_PLAN.clear()
    _STAMP_PLAN.update(build_plan())
    return len(_STAMP_PLAN)
