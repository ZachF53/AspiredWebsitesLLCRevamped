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

## Rehearsal findings, 2026-08-16

The drop was rehearsed against a seeded rehearsal database — promoting these
files, running them, and watching them fail somewhere harmless. It did not
survive contact:

1. **A single `clients` migration cannot do it.** `RemoveField` has to live
   in the app that owns the model. All 51 columns in one `clients`
   migration died on `KeyError: ('clients', 'aiassistantlog')`. The files
   here are now split per app — 8 phase-1 migrations plus one phase-2.
2. **Constraints and indexes must be dropped before the columns they
   cover.** Removing `reporting.GbpPerformanceSnapshot.client` failed with
   `FieldDoesNotExist: NewGbpPerformanceSnapshot has no field named
   'client'` during SQLite's table rebuild, because a `unique_together`
   or index still references it. Every model with a composite constraint
   touching `client` or `project` needs an `AlterUniqueTogether` /
   `RemoveIndex` operation emitted ahead of its `RemoveField`. That is not
   yet generated here.
3. **Ordering is load-bearing.** Phase 2 must depend on every phase-1
   migration, or the tables are dropped while other apps' FK constraints
   still point at them.

None of that had reached the application-level breakage yet — those are
schema errors that occur before any code runs. The 57 modules still
reading `ClientProfile` are a separate, larger problem waiting behind them.

Take this as the argument for why the drop is a project, not a command.

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
