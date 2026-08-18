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
   `RemoveIndex` operation emitted ahead of its `RemoveField`.

   **Resolved for the unique constraints, 2026-08-17.** All eight
   `unique_together` tuples keyed on `client` were re-keyed onto
   `website_new` in `clients.0056` and `reporting.0017`. That was not
   done to unblock the drop — it was forced by making `client` nullable.
   A NULL is distinct from every other NULL in a unique index, so the
   moment writers stopped setting `client`, those constraints silently
   stopped enforcing anything. `reporting.GbpReview` was the sharp edge:
   the review sync upserts on that key every four hours, so the guard
   failing open meant a duplicate copy of every review six times a day.
   Fixing the correctness bug removed the migration blocker as a side
   effect.

   **Resolved for the plain indexes too, 2026-08-18.** All twelve
   `models.Index` entries keyed on `client` now have a `RemoveIndex`
   ahead of their `RemoveField` in the phase 1 files, and
   `clients.tests_planned_migrations` fails if a thirteenth is added
   without one.

   Removing them turned out not to be the cost-free bookkeeping the
   note above assumed. Every one was a composite `(owner, timestamp)`
   index, and once the readers moved to `website_new` / `account_new`
   the index covered a column nothing queried while the queries that
   replaced it ran with only the single-column FK index behind them —
   no help at all for the ordering half. `clients.UptimeRecord` is the
   sharp edge: ~75k rows, and `get_uptime_chart_data` issues 90
   `(website_new, checked_at)` queries per call. Nothing fails, so
   nothing reports it; the dashboard just gets slower every month.

   So each index was **mirrored onto the canonical column first**
   (`clients.0058`, `reporting.0018`, `domains.0006`) and only then
   queued for removal. `clients.tests_canonical_coverage` asserts the
   mirror exists, which is the index-level version of the column-level
   check below.
3. **Ordering is load-bearing.** Phase 2 must depend on every phase-1
   migration, or the tables are dropped while other apps' FK constraints
   still point at them.

None of that had reached the application-level breakage yet — those are
schema errors that occur before any code runs. The modules still reading
`ClientProfile` are a separate, larger problem waiting behind them.

Take this as the argument for why the drop is a project, not a command.

## Column-level findings, 2026-08-17

The parity audit compares fields that exist on **both** sides, so a field
that exists on only the legacy side is invisible to it by construction.
Six were, and every one was written by live code and read by a scheduled
task. The drop as staged would have taken all six:

| Legacy field | Moved to | What was at stake |
|---|---|---|
| `gbp_location_name` | `Website` | the Google listing binding |
| `do_snapshot_id` | `Website` | the 60-day retention snapshot's id — an orphaned, still-billed DigitalOcean resource nothing could find or restore from |
| `site_status` | `Website` | live / maintenance / offline / destroyed |
| `payment_failure_started_at` | `Account` | the escalation guard |
| `payment_failure_offenses` | `Account` | 1st-free / 2nd-costs-$75 |
| `stripe_social_subscription_id` | `Account` | the social plan's Stripe link |

Four models also carried a legacy FK with **no canonical counterpart at
all** — `social.ScheduledPost`, `reporting.GbpReview`,
`reporting.GbpPerformanceSnapshot`, `admin_dashboard.AIAssistantLog`. The
last is the starkest: an append-only audit trail that would have kept
every row and lost, on all of them, the record of who the action was
performed against.

`clients.tests_canonical_coverage` now asserts the property directly, so
a seventh cannot be added quietly.

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
