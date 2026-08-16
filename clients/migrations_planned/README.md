# Planned legacy-removal migrations — NOT in the migration graph

This directory is deliberately **not** a Django migrations package. There is
no `__init__.py`, the files carry a `.planned` suffix, and the directory name
is not `migrations`, so `manage.py migrate` cannot discover or execute
anything here.

That is the point. A destructive migration sitting in `clients/migrations/`
would be applied by the next routine deploy — the deploy script runs
`migrate` unconditionally. Dropping `ClientProfile` and `Project` is a
separately approved operation with its own backup and rollback plan, not
something that rides along with a template fix.

## What is planned

51 legacy FK columns and 2 tables. Removal happens in three migrations, in
this order, because reversing the order breaks referential integrity:

1. **Drop the dependent legacy FK columns** (49 of them). Every dependent
   row already carries its canonical `account_new`/`website_new` FK, so
   these columns are pure duplication at that point.
2. **Drop the transitional links** — `Account.legacy_client_profile` and
   `Website.legacy_project`. These exist only so the backfill stays
   reversible; once the legacy tables go, they point at nothing.
3. **Drop the tables** — `clients_project`, then `clients_clientprofile`
   (Project FKs ClientProfile, so Project goes first).

## Preconditions — all must hold before any of this runs

Verify with `manage.py check_legacy_removal_readiness`:

- Strict parity gate passes with `--fail-on-warnings`, twice, with a
  zero-write backfill pass between the runs.
- No runtime code reads `ClientProfile` or `Project` as a canonical source.
  The readiness command reports the remaining reader count; it is not zero
  today.
- A verified, restorable PostgreSQL backup exists.
- A PostgreSQL→PostgreSQL migration rehearsal has been timed on a restored
  copy of production. The SQLite rehearsal used elsewhere in this project
  validates data mapping only — it cannot tell you how long an `ALTER TABLE`
  holds a lock on a live table, and `clients_uptimerecord` has ~75k rows.
- Waves 1–5 have been deployed and observed in production.

## Promotion

When every precondition holds, copy the `.planned` file into
`clients/migrations/`, drop the suffix, set `dependencies` to the current
leaf migration, and run it against a restored copy first.
