# Account/Website Cutover Contract

Status: stabilization working document. This file describes the intended
Phase-D destination and the gates that must pass before legacy tables are
removed. It is not authorization to drop data.

## Objective

Replace the runtime use of `ClientProfile` and `Project` with one canonical
ownership hierarchy:

```text
User -> Account -> Website(s)
```

The cutover is complete only when runtime readers and writers use `Account`
and `Website`, every dependent row has an unambiguous canonical owner, and a
production-like migration rehearsal passes the parity audit.

## Canonical ownership

### Account level

- Login user and account-holder identity
- Contact and mailing/WHOIS details
- One Stripe customer ID
- Communication preferences
- Account status and internal notes
- Client vault/PIN ownership
- Moonieful person/client identity and sync state
- Referral relationship
- Account-level comp decisions
- Subscription payment-method overrides

### Website level

- Business/brand name and business type
- Live and staging URLs
- Build/lifecycle stage
- Build package and payment state
- Intake, contracts, revisions, documents, and project activity
- Launch date and support window
- Droplet/build hosting relationship
- Hosting and maintenance subscription IDs
- Analytics, reporting, uptime, scans, content, and security history
- Moonieful website handoff state

### Service records

- `MaintenancePlan` is the canonical maintenance entitlement and billing row.
- `SocialMediaPlan` is the canonical social entitlement and billing row.
- `Droplet` is the canonical DigitalOcean resource row; `website` may be null
  only for a genuinely account-level move-over/self-provisioned server.
- Stripe customer identity belongs to `Account`; subscription metadata must
  carry enough account/website context to route webhooks deterministically.

## Decisions required before destructive migrations

1. ~~**Account display identity**~~ — **decided 2026-08-16.** `Account.name`
   is the client's billing/account organization name, normally the firm
   name. `Account.contact_name` is the person; `Website.name` is the
   individual brand/site. The Account name is never derived from the first
   Website. Implemented as `clients.account_setup.account_name_for`, which
   every writer (signal, both backfills, the parity validator) calls.
2. **Historical multi-website allocation:** never silently attach legacy
   website-level rows to the oldest website when an account has more than one.
   Produce a manual mapping for every such account.
3. **Stage-log destination:** confirm whether existing `ProjectStageLog` rows
   are retained with a required Website FK or copied into `WebsiteStageLog`.
   Do not keep two append-only histories active.
4. **Legacy project linking:** signal-created Websites currently may have no
   `legacy_project`. Every Project must be explicitly linked or declared
   disposable before Project is removed.
5. **Subscription precedence:** where Website subscription fields and service
   plan rows disagree, define which Stripe ID/status wins and reconcile it
   before switching webhooks.

## Non-negotiable invariants

- Every legacy `ClientProfile` has exactly one linked `Account` until the
  legacy profile is removed.
- Linked Account and ClientProfile rows reference the same Django user.
- Every legacy Project has exactly one mapped Website.
- A Website mapped to a Project belongs to the Account mapped from that
  Project's ClientProfile.
- Every transitional `account_new`/`website_new` FK is populated wherever a
  legacy owner is present and a canonical owner exists.
- Nonblank Stripe customer, subscription, and DigitalOcean IDs are not
  duplicated across canonical owners.
- Moonieful UUIDs remain unique and Moonieful intake JSON remains the source
  of truth for synced clients.
- Vault credentials never lose their Account owner or encryption metadata.
- Launch remains blocked until final payment is confirmed.

## Validator

Read-only human report:

```powershell
python manage.py audit_account_website_parity
```

Machine-readable output:

```powershell
python manage.py audit_account_website_parity --json
```

Deployment/rehearsal gate:

```powershell
python manage.py audit_account_website_parity --strict --fail-on-warnings
```

Findings come in three severities:

- **error** — a structural break. Blocks the gate.
- **warning** — drift or an unreviewed decision. Blocks with
  `--fail-on-warnings`.
- **operational** — the structure is sound but the business state needs a
  human to check it against the real world. Does not block the migration
  gate; blocks the affected site instead. Add `--fail-on-operational` to
  treat them as blocking.

The one operational check today is
`website-fully-paid-without-ledger-evidence`: a site marked `fully_paid`
with no payment record, deposit/final timestamp, Stripe invoice, or signed
contract behind it. `fully_paid` is what releases the launch gate, so
`clients.services.change_client_stage` refuses to move such a site to
`live` until an operator passes `payment_verified=True` to record that they
checked. Nothing is ever fabricated to clear this — no PaymentRecord,
timestamp, contract, or Stripe object is invented to make the books
balance.

The strict command must pass after the rehearsal backfill, then pass again
after the backfill is rerun as a no-op. An empty database producing a
zero-finding report is not evidence that production data is ready.

## Rehearsal

No production backup was available, so the rehearsal runs against a
synthetic dataset built to production's shape — direct build clients, a
Moonieful referral, a multi-website account, a subscription-only buyer,
contracts, payments, domains, droplets and vault credentials — carrying the
structural defects real data carries.

The rehearsal database is a dedicated SQLite file. `settings_rehearsal`
ignores `DATABASE_URL` outright, refuses to start unless the filename says
"rehearsal", and blanks every outbound credential so a signal firing during
a backfill cannot bill a customer or provision a droplet.

```powershell
$S = "--settings=AspiredWebsitesRevamped.settings_rehearsal"
python manage.py migrate $S
python manage.py seed_rehearsal_dataset --fresh $S
python manage.py audit_account_website_parity --json $S      # baseline

python manage.py repair_account_website_parity --apply $S    # user + link fixes
python manage.py refactor_to_accounts $S
python manage.py backfill_website_fks --apply $S
python manage.py backfill_account_data --apply $S

python manage.py repair_account_website_parity --emit-manifest repair.json $S
#   ... fill in every null in repair.json ...
python manage.py repair_account_website_parity --manifest repair.json --apply $S

#   ... rerun the four backfills; they must report zero changes ...
python manage.py audit_account_website_parity --strict --fail-on-warnings $S
```

Result of the 2026-08-16 synthetic run: 59 error occurrences and 11 warnings
at baseline, 0 and 0 after repair, second pass wrote nothing, gate exited 0.

### Real-data rehearsal (2026-08-16)

Run against a read-only snapshot of production (10 profiles, 10 accounts,
12 websites, 8 projects), restored into an isolated SQLite rehearsal
database and destroyed afterwards. Production was never written to.

The snapshot is taken with Django's serializer rather than `pg_dump`,
because a PostgreSQL dump cannot be restored on a workstation with no
PostgreSQL, and a SQL-level PG→SQLite transfer corrupts this schema
outright: Django stores UUID primary keys as 32-char hex on SQLite and as
native `uuid` on PostgreSQL, so every key would arrive mangled.
`dumpdata`/`loaddata` converts per field and preserves primary keys.

> #### ⚠️ This does NOT replace a PostgreSQL→PostgreSQL rehearsal
>
> A serializer round-trip into SQLite validates **application-data
> mapping**: which row owns which, whether the backfills move the right
> values, and whether the parity gate can reach zero. That is what it was
> used for, and for that purpose it is sound.
>
> It cannot validate anything the database engine itself decides, because
> the objects under test never existed in this run:
>
> - migration DDL against real PostgreSQL — `ALTER TABLE`, constraint
>   drops, index rebuilds, and how long each holds a lock on a live table
> - foreign-key and unique constraints as PostgreSQL enforces them,
>   including deferrability and `ON DELETE` behaviour that SQLite applies
>   more loosely
> - native column types with no SQLite equivalent (`uuid`, `jsonb`,
>   timezone-aware timestamps) and their casts
> - collation, ordering, and case sensitivity differences that change
>   which row a `.first()` returns
> - transaction and locking behaviour, and whether a destructive migration
>   can run without taking the site down
> - row volume and query plans at production scale
>
> **Before any destructive schema removal — dropping `ClientProfile`,
> `Project`, or the `_new` FK columns — rehearse the migration on a
> restored PostgreSQL copy of production, on a machine with PostgreSQL,
> and time every DDL step.** Passing the gate here is a precondition for
> that rehearsal, not a substitute for it.

Restoring a snapshot requires the `raw=True` guards now present on every
`post_save` receiver — without them, loading a fixture re-fires business
logic and tries to create a second Account for a user who is about to get
theirs from the fixture.

**Production's structural state is clean:** 0 errors, no duplicate Stripe
or DigitalOcean identifiers, no orphaned Projects, no unallocated rows, and
no row mis-filed by the old oldest-wins rule. The backfills had nothing to
repoint.

**The tooling was not clean.** Blanket refresh from the legacy profile
rewrote 10 of 12 Websites, and on a live paying client it reverted
`payment_status` from `fully_paid` to `awaiting_deposit`, erased the live
`url`, cleared `package` and `business_type`, reset `revision_count`, and
turned `maintenance_active` off. `do_droplet_name` was wiped on nine sites
because the legacy profile has no such column and the mapping supplied `''`.
Backfills now fill gaps only (`_fill_missing`) and never overwrite a
populated canonical value. Same run after the fix: 2 gap-fills, both
genuine.

Final state: gate passed, backfills rerun with zero writes, gate passed
again.

### What the rehearsal caught

Three defects in the migration tooling itself, none of which the validator
can detect after the fact:

1. **`refactor_to_accounts` duplicated every Website.** Its idempotency key
   was `(account, legacy_project)`, but the Phase-C autocreate signal
   creates Websites with a null `legacy_project`, so the key never matched.
   Eight websites became sixteen, with the duplicates taking `-2` slugs —
   which are the portal URLs. It now adopts the signal-created row.
2. **Legacy rows were mis-filed on multi-website accounts.** Both
   `refactor_to_accounts` and `backfill_website_fks` assigned every
   client-level row to the account's oldest Website. A Vance *Mediation*
   support ticket landed under Vance *Family Law*, and a populated-but-wrong
   FK is indistinguishable from a correct one afterwards. Both now resolve
   through the row's own `project` FK, and leave the row null when they
   cannot tell — so the audit keeps reporting it until a human maps it.
3. **Subscription-only buyers got a phantom build Website.**
   `_client_has_website_data` treated `maintenance_active` as evidence of a
   build, re-creating exactly the row `clients.signals` deliberately
   declines to make for them.

Plus two idempotency bugs that made "rerun changes nothing" unprovable: the
two backfills disagreed forever over `client_pin_salt` (`b''` vs `None`),
and every run rewrote every column, bumping `updated_at` — which the
Moonieful bridge compares to decide whether an inbound record is stale.

### Gaps vs conflicts

The validator's question is not "do the legacy and canonical rows agree?"
Both are written during the transition, so they often will not. The
question is what breaks when the legacy row is dropped:

- **gap** — canonical empty, legacy holds a value. The value disappears at
  drop time. The backfill fixes this on its own.
- **conflict** — both hold different real values. Only one survives, and
  neither store is universally authoritative right now (the portal writes
  Account and Website; legacy paths still write ClientProfile). Reported
  for a decision, never auto-resolved.
- **stale** — legacy empty, canonical populated. Expected, and not a
  finding; the canonical row simply moved on.

`False` and `0` count as real values, not emptiness. Treating them as empty
is how a stale legacy row silently resets a revision counter or flips a
feature toggle.

### Decisions the tooling will not make for you

`repair_account_website_parity` reports and refuses rather than guessing.
Each of these needs a named answer in the manifest:

- which row keeps a duplicated Stripe customer, subscription, or droplet ID
  (row age is not evidence — `--emit-manifest` records payments collected,
  plan state, live site and droplet state so the call is made on facts;
  `--prefer-oldest` is for synthetic fixtures only)
- which side wins a legacy/canonical field conflict
- which Website an unallocatable legacy row belongs to
- what becomes of a legacy Project that never got a Website
- an Account/user conflict where both users hold an Account (a merge)

`repair_account_website_parity` never calls Stripe or DigitalOcean. It
clears a local column so two rows stop claiming one remote object; no
subscription is cancelled and no droplet is destroyed. Reconciling the
remote side is a separate, deliberate act.

A completed multi-website mapping is recorded on
`Account.multi_website_reviewed_at`, and a Website added after that
timestamp reopens the warning — the mapping never considered the new site.

## Cutover waves

1. Account creation, editing, authentication, and portal resolution
2. Contracts, intake, delivery stages, revisions, documents, and support
3. Stripe checkout/webhooks, payments, subscriptions, domains, droplets, vault
4. Moonieful sync, scheduled tasks, social services, and follow-ups
5. Reporting, analytics, uptime, scans, GBP, and session recording

For each wave: characterize behavior, switch reads and writes together, run
targeted tests and parity checks, deploy, observe, then remove that wave's
legacy fallback. Legacy tables are dropped only after every wave is complete.

### Status, 2026-08-19

**The code half of all five waves is done and on `main`.**
`manage.py check_legacy_removal_readiness` reports zero live code reads
and zero template findings, and answers "All checkable preconditions
satisfied." No runtime path reads `ClientProfile` or `Project` as a
canonical source, and the per-wave legacy fallbacks are gone rather than
merely unused.

#### The gate has been confidently wrong five times

Each time it answered "zero" while a whole category sat outside what it
could see. Worth reading before trusting any number it prints:

| Blind spot | Why it was invisible | Found |
|---|---|---|
| prose in docstrings | substring grep, no parser | over-reported 21 of 70 |
| `request.client_profile` | an attribute holding a legacy instance; names no class | 21 reads in one module, and `domains/views.py` never parsed |
| `legacy_client_profile` | the transitional FK itself | 17 reads across 6 modules |
| `*.html` | the scan parses Python | 22 templates, 17 of them 500s |
| `x.client.firm_name` | `client` is an ordinary attribute name; only the trailing attribute tells | 28 reads |

The pattern is the same every time: **the gate counts symbols, and a
dependency does not have to name anything.** When it says zero, the
useful question is "zero of what it can see", and the answer has been
"less than I assumed" five times out of five.

Note the first row is the gate being wrong in the *other* direction —
over-reporting, because a docstring mentioning `ClientProfile` was counted
as a dependency. A gate that inflates its own blocker count is a gate the
reader stops believing, which is worse than no gate.

One more thing measurement taught:

- **A passing suite is not an observed wave.** 883 tests passed while the
  live portal sat in an infinite redirect loop, because every individual
  redirect was correct and only the chain was wrong. Rendering real pages
  with `follow=True` is what caught it. "Deploy, observe" means requesting
  pages, not reading a green build.
- **Converting the Python does not convert the templates, and the
  readiness gate cannot see them.** It parses `.py` files, so twenty-two
  templates naming the owner through the legacy FK counted as zero
  blockers. Seventeen dereferences sat inside `{% url %}`, where an empty
  argument raises NoReverseMatch and 500s the page; thirty-eight were
  plain `{{ }}`, which Django resolves to the empty string — HTTP 200, no
  log line, blank cell.
- **Existing data hides both.** Rows written before the cutover still
  carry a legacy FK, so every one of those lookups resolves on staging
  and the pages look fine. Only a fixture with no legacy row anywhere
  exposes it — `admin_dashboard/tests_canonical_only_render` builds
  exactly that, and 11 of 14 pages failed the first time it ran. That
  fixture is also the shape of every client created since the cutover,
  so the same pages are wrong in production now, not only after the drop.

#### What testing the post-cutover shape found

Every client created since the cutover has an Account and a Website and
no legacy row. Nothing had ever rendered or run against that shape,
because production data all predates it — so the lookups resolve, the
pages look fine, and the bugs are invisible until a new client hits
them. Building fixtures with no legacy row anywhere turned up, among
others:

- three Celery beats writing **zero rows a night** — health scores,
  monthly intelligence, competitor gaps — each returning quietly
- `social.publish_due_posts` raising inside its own `except` block,
  so one canonical-only client's failed post stopped social publishing
  for every paying social client, every five minutes
- the deposit webhook raising before recording the payment
- no stage-change email, no final invoice at pre-launch, no tracker
  snippet, no chatbot, no NPS survey, no upsell nudge
- a delete-impact modal reporting "0 tickets, 0 documents" immediately
  before the admin types the account name to confirm deletion

Several were broken for **everyone**, not only new clients: the vault
"Open the vault" link, the freshness-crawl button, the session-recording
toggle, and `portal_subscriptions` for any comped account.

**Remaining before the drop** (none of it code):

- Deploy to production and observe. Staging has run the full cutover
  since 2026-08-17; production is well behind and carries several of the
  live bugs above.
- A verified, restorable PostgreSQL backup.
- A timed PostgreSQL→PostgreSQL rehearsal on a restored copy of
  production. The SQLite rehearsal validates data mapping only; it cannot
  measure how long `ALTER TABLE` holds a lock, and `clients_uptimerecord`
  carries ~75k rows.
- The three data decisions in "Decisions the tooling will not make for
  you" that live production rows still raise.

## Public brand workstream

The public truth, conversion, policy, proof, and accessibility work from
`BRAND_REMEDIATION_HANDOFF.md` runs alongside these cutover waves. Its exact
release placement, dependencies, decision gates, and acceptance criteria are
defined in `docs/integrated_stabilization_brand_roadmap.md`. Unverified
business and legal facts are tracked in `docs/brand_fact_matrix.md` and are
not implementation defaults.
