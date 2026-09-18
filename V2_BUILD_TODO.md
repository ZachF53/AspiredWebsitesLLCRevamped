# V2 Dashboard Build — TODO

Branch: `v2-dashboard`. Do not deploy. Additive only — see CLAUDE.md hard
constraints in the build prompt for the full list.

## Phase 0 — Verify ground truth

- [x] 0a. `_on_intake_submitted` (clients/views.py) — CONFIRMED, worse than stated.
      Line 946: `profile.onboarding_status = 'onboarding_complete'` — `profile`
      is a `Website` here (per docstring + call site `intake()` at
      clients/views.py:643-644, where `profile = request.website` and
      `project = _active_project(request)`, and `_active_project` returns
      `request.website` when set — i.e. `profile is project`). `'onboarding_complete'`
      is NOT a valid `Website.ONBOARDING_STATUS_CHOICES` value (valid:
      `pending_intake` / `intake_complete` / `complete` — account_models.py:304-308).
      The guard at lines 972-977 (`if project.onboarding_status == 'pending_intake':
      project.onboarding_status = 'intake_complete'`) never fires because by the
      time it runs, `project` (== `profile`, same in-memory object) already has
      `onboarding_status == 'onboarding_complete'` from line 946 — so the
      condition is always False. Confirmed exactly as briefed.
      EXTRA FOUND (not in brief): line 962 also sets
      `account.onboarding_status = 'onboarding_complete'`, which is ALSO invalid
      for `Account` (valid: `pending_setup` / `complete` only — account_models.py:97-100).
      Both writes persist an out-of-choices string to the DB.
- [x] 0b. navigation.py context processor — CONFIRMED. Registered globally at
      settings.py:164 (`admin_dashboard.navigation.navigation`), in
      TEMPLATES/context_processors, so `admin_nav` is available on every
      render. `admin_dashboard/templates/admin_dashboard/base.html` renders it.
      vault templates (`totp_setup.html`, `ops_session_replay.html`,
      `client_vault.html`, `command_library.html`, `enter_pin.html`, etc.) all
      `{% extends "admin_dashboard/base.html" %}`, so the sidebar renders there
      too. Confirmed — this is exactly why the toggle must be a session flag,
      not a URL prefix.
- [x] 0c. CONFIRMED. `clients/services.py:change_client_stage` (line 60-64)
      raises `GuardError` for stage `'live'` unless `payment_status ==
      'fully_paid'`, plus a second unverified-payment-evidence check
      (line 71-80) unless `payment_verified=True`. `admin_dashboard/views.py
      :website_change_stage` (line 4492) has NO such check anywhere — it
      writes `website.stage = new_stage` unconditionally once the stage name
      is valid. Confirmed exactly as briefed.
- [x] 0d. CONFIRMED. No admin view renders a client's submitted `IntakeResponse`
      answers/photos. The only IntakeResponse-adjacent admin UI is a
      write-only manual override button on `website_detail.html` (around line
      4427-4457 in admin_dashboard/views.py) that FLIPS completion flags
      without ever displaying what the client answered. `clients/admin.py`
      has a bare Django-admin `IntakeResponseAdmin` (list_display only, no
      photo rendering) — not part of the operator-facing dashboard.
- [x] 0e. CONFIRMED SCHEDULED — not manual-only. `reconcile-subscriptions` is
      in `CELERY_BEAT_SCHEDULE` (settings.py:884-887), daily at 4am, running
      `billing.tasks.reconcile_subscriptions_task` ->
      `manage.py reconcile_subscriptions`. **CHECKPOINT:** this command
      queries every `Website` with a non-empty `stripe_hosting_subscription_id`
      and cancels the Stripe subscription if `_droplet_alive()` is False for
      that site's droplet. Read the code (billing/management/commands/
      reconcile_subscriptions.py) — it only acts when the droplet is
      confirmed dead, so it should not touch Burgland Tech / Denis Law Group
      as long as their droplets are up. Not modified in this build (out of
      scope, lives in billing/). Flagged here per instructions because it is
      an existing automated write path that reaches the two live subscriptions
      and runs unattended overnight regardless of this build.

All Phase 0 assumptions held. Proceeding.

## Phase 1 — Fix intake status bug (clients/, shared code) — DONE

- [x] 1a. Fixed `_on_intake_submitted` (clients/views.py). When `profile` is a
      `Website` (the new-flow aliased call), it now advances
      `pending_intake` -> `intake_complete` in place instead of stomping
      `'onboarding_complete'`. When `profile` is the legacy `ClientProfile`,
      unchanged (`'onboarding_complete'` stays valid there). Also fixed the
      extra bug found in Phase 0: `Account.onboarding_status` now gets
      `'complete'` instead of the invalid `'onboarding_complete'`. The later
      "mirror onto Website" block is kept for the legacy call shape
      (`project is not profile`) — harmless no-op in the new-flow case since
      profile/project already share the fix.
- [x] 1b. `clients/management/commands/fix_stuck_onboarding_status.py` —
      scans both `Website` and `Account` for out-of-choices values, reports
      only, never writes (no `--apply` path exists at all in this build).
- [x] 1c. Tests: `clients/tests_intake_submit.py::test_on_intake_submitted_marks_both_records`
      updated to assert `intake_complete`/`complete` (was asserting the bug);
      new `clients/tests_fix_stuck_onboarding_status.py` (4 tests) covers the
      command finding Website/Account drift, reporting cleanly when none
      exists, and never writing. `clients/tests_portal_gate.py` (7 tests) run
      unmodified and pass, confirming the access gate is unaffected.
- [x] 1d. Verified by reading `clients/decorators.py:103-109` — the gate's
      only comparison is `request.website.onboarding_status ==
      'pending_intake'`; every other value (old buggy `'onboarding_complete'`
      or the new correct `'intake_complete'`/`'complete'`) falls through to
      the same `else: status = 'onboarding_complete'` (a local sentinel
      variable, unrelated to the model field) and is admitted. Confirmed by
      code reading and by `tests_portal_gate.py` passing unmodified.

Targeted run: `python manage.py test clients.tests_intake_submit
clients.tests_fix_stuck_onboarding_status clients.tests_portal_gate` — 18/18
pass.

**Found but NOT fixed (pre-existing, out of scope):**
`clients.tests.GmbIntakeFollowupTests.test_have_sends_add_manager_and_creates_todo`
and `test_need_sends_create_and_creates_todo` were ALREADY FAILING before
any change in this build (confirmed via `git stash` against the Phase-0-only
commit). `_on_intake_submitted` tries `SiteChangelogEntry.objects.create(
website_new=profile, ...)` where `profile` is a legacy `ClientProfile`
(that test's call shape), but `website_new` is FK'd to `Website` — raises
`ValueError`, caught by the surrounding best-effort `except Exception`, but
something downstream of that (the GMB SetupTodo creation) never runs as a
result, so `_has_todo()` is False. Unrelated to the onboarding_status bug
this phase targets; left alone per "additive only" / narrow-scope rules.

## Phase 2 — v2 scaffolding — DONE

- [x] 2a. `admin_dashboard/v2/` package (services, views_*, urls, templates
      dir at `admin_dashboard/templates/admin_dashboard/v2/`). **Mounted at
      `/admin-dashboard/v2/`, not `/dashboard/v2/`** — the app's real mount
      point (settings root urls.py) is `/admin-dashboard/`, established
      before this build; `/dashboard/...` in the brief is read as shorthand
      for that existing prefix. Same substitution applies to the toggle
      routes below. Every v2 view uses the existing `admin_required`
      (imported from `admin_dashboard.decorators`, never redefined).
- [x] 2b. Toggle: session key `dashboard_version` (default `'v1'`), routes
      `/admin-dashboard/use-v2/` and `/admin-dashboard/use-v1/`
      (`admin_dashboard/v2/views_toggle.py`, registered directly in
      `admin_dashboard/urls.py` since they sit outside `/v2/`).
      `navigation.py`'s `navigation()` context processor branches on the
      flag and builds `admin_nav` from `NAVIGATION_V2` instead of
      `NAVIGATION` when set — `NAVIGATION` itself is never modified. The
      "View New/Old Dashboard" link is injected as a plain item on the
      first nav group by `navigation()`, not added to base.html's markup —
      base.html's sidebar loop is already generic over `admin_nav`, so v1's
      template needed zero edits. Verified with
      `ToggleTests.test_v2_flag_follows_into_vault` — the v2 sidebar
      renders on `/admin-dashboard/vault/` after toggling, which is the
      entire reason the brief called for a session flag over a URL prefix.
- [x] 2c. `NAVIGATION_V2`: Dashboard, Accounts, Websites, Domains, Billing,
      Vault — verified as an exact ordered list by
      `NavigationV2DefinitionTests.test_exactly_six_items_in_this_order`.
- [x] 2d. `tests_navigation.py` extended with `NavigationV2DefinitionTests`
      and `ToggleTests`; no v1 assertion changed.
- [x] 2e. Every v2 template `{% extends "admin_dashboard/base.html" %}` —
      no separate v2 base template exists. Reuses existing classes
      (`admin-card`, `lead-table`, `lead-filter-bar`, `status-badge`,
      `btn-*`, `form-*`) throughout. New CSS is confined to one clearly
      marked `/* ── v2 dashboard ── */` section appended to the end of
      `core/static/css/main.css` (action list rows, stat tiles, tab bar,
      a key/value table, a photo grid, an onboarding-step row) — nothing
      existing was touched.

## Phase 3 — Landing dashboard (`/admin-dashboard/v2/`) — DONE

- [x] 3a. Merged, urgency-sorted (longest-waiting-first) action list in
      `services.get_action_list()`: dunning approvals (`DunningEvent`
      awaiting_approval), unresolved `SystemAlert` rows, unreviewed
      `VulnerabilityScan` rows with critical/high findings, `Website` rows
      `status='active'` but `site_status != 'live'`, `OnboardingInvoice`
      rows sent 30+ days ago and still unpaid (no due_date field exists
      anywhere in billing — documented as sent-anchored, not due-date
      anchored), `Account` rows `onboarding_status='pending_setup'`,
      `Website` rows `onboarding_status='pending_intake'`, and `Website`
      rows with `needs_admin_review_at` set (same filter v1's Needs You
      queue uses — reused, not reinvented). **SSL cert expiry skipped
      entirely** — confirmed via repo-wide grep that no model/field
      anywhere tracks TLS certificate expiry (only domain-registration
      expiry and a point-in-time SSL Labs scan grade exist, neither of
      which is "certificate expires on X"). Each source is wrapped in
      try/except so one bad query can't blank the list.
- [x] 3b. Counts block: total clients, active websites, paying clients
      (accounts with any `maintenance_active=True` website), pending
      setup, pending intake.
- [x] 3c. Money block: MRR + cash collected this month, HTMX
      `hx-trigger="load"` below the fold, 5-minute Django cache. **Reads
      local ledger data only** (`clients.revenue.get_current_mrr()` +
      a `PaymentRecord` sum) — see decision log below for why this is
      NOT a live Stripe API read despite the brief's wording.
- [x] 3d. `admin_dashboard/v2/services.py` holds every query; views only
      fetch and render.

## Phase 4 — Accounts — DONE

- [x] 4a. `/admin-dashboard/v2/accounts/` — searchable (name/contact/email),
      sortable, shows website count and per-account MRR (derived from
      `get_current_mrr()`'s breakdown, not reimplemented).
- [x] 4b. Detail page: editable contact/billing form (name, contact_name,
      phone, email_alt, address/city/state/zip — Stripe customer ID shown
      read-only, deliberately not editable); "Send account setup email"
      button calls the existing `clients.emails.send_onboarding_setup_email`
      with a 24h cooldown against `OnboardingToken.last_setup_reminder_at`
      (existing field, no migration needed); shows "setup complete" state
      instead of the button once `onboarding_status == 'complete'`; never
      fires on account creation. Password reset via Django's
      `PasswordResetForm` — see decision log for why this is a local
      reimplementation of v1's `account_send_password_reset` rather than
      a call to it.
- [x] 4c. `/admin-dashboard/v2/accounts/new/` — creates an inactive `User`
      + `Account` only. Sends nothing (verified:
      `test_account_create_post_creates_record_and_sends_nothing` asserts
      `len(mail.outbox) == 0`).

## Phase 5 — Websites (core) — DONE

- [x] 5a. `/admin-dashboard/v2/websites/` — searchable, filterable by stage.
- [x] 5b. `/admin-dashboard/v2/websites/<id>/` tabs, all built:
      **Overview** (status/stage/URLs/launch date/droplet IP/maintenance;
      stage control via the GUARDED `clients.services.change_client_stage`,
      never the ungated v1 `website_change_stage`; labeled manual override
      for off-Stripe payments — required reason field, reuses the existing
      `verify_website_payment` management command via `call_command` for
      the attestation, then sets `payment_status='fully_paid'` and calls
      `mark_live`). **Onboarding** (Contract/Payment/Account setup/Intake,
      each collapsing to a status line once done; intake reminder resend
      throttled 48h against `OnboardingToken.last_intake_reminder_at` —
      v1's button has no such throttle; Contract/Payment incomplete states
      show disabled "Coming soon" buttons — see below). **Intake** (every
      submitted `IntakeResponse` field + inline `IntakePhoto` images,
      downloadable — did not exist anywhere in the admin before this).
      **Infrastructure** (hidden entirely when `build_platform ==
      'wordpress'`; links to the existing Droplet metrics/power page,
      Deploy runbook, and Vault ops sessions rather than duplicating power
      controls). **Security** (run-scan creates a `VulnerabilityScan` +
      enqueues the existing Celery task, exactly mirroring v1's
      `run_scan`; auto-send toggle flips the existing
      `Website.auto_send_scan_reports` field; scan history links to v1's
      `scan_detail` for the PDF/findings view). **Domains** (read-only,
      links to v1's `admin_domain_detail` for repoint/DNS). **Billing**
      (read-only subscriptions + invoices; plan-change/cancel are disabled
      "Coming soon"). **Monitoring** (uptime records, changelog,
      session-recording status — no recording viewer exists yet, noted
      below).
- [x] 5c. Verified against FOUR website states (brand new / live /
      wordpress / archived) via `tests_v2_smoke.py` — every tab, every
      state, 200 OK, no crash.

## Phase 6 — Domains / Billing list pages — DONE

- [x] 6a. `/admin-dashboard/v2/domains/` — all `DomainRegistration` rows,
      status, expiry, attached website. Read-only; links to v1's
      `admin_domain_detail` for the actual repoint/DNS actions (no
      transfer-out, no delete in v2, per the brief).
- [x] 6b. `/admin-dashboard/v2/billing/` — unpaid `OnboardingInvoice` +
      open `MiniInvoice` rows across every client, oldest first. Read-only,
      existing data only.

## Phase 7 — Verification — DONE

- [x] 7a. Walked every item above by hitting the actual URL/test, not from
      memory — `tests_v2_smoke.py` (25 tests) exercises every v2 page,
      every website-detail tab across 4 website states, the stage guard,
      the payment override, the live-subscription block, and the
      auto-send toggle.
- [x] 7b. v1 unchanged: `tests_navigation.py`'s original v1 assertions
      (unedited) pass — 41-item nav renders
      (`test_sidebar_renders_every_item`), single `aria-current` marker,
      group labels render. `ToggleTests` confirm the toggle moves between
      v1 and v2 and that the v2 flag survives into Vault.
- [x] 7c. `git diff main...v2-dashboard | grep 'stripe\.'` — zero hits in
      actual code (the only match was this checklist line describing the
      check itself).
- [x] 7d. Found a real gap: `website_stage` (stage changes + the payment
      override) and `website_toggle_auto_send_scan` write Website fields
      directly and were NOT checking for a live subscription id. Fixed —
      added `_block_if_live_subscription()`, checked before any write in
      both views, plus a visible banner/disabled controls in the
      template. Three new regression tests confirm the block. NOT
      guarded (documented, not fixed): `website_run_scan` (creates a
      related `VulnerabilityScan` row via FK — never writes the Website
      row itself) and `website_send_intake_reminder` (writes
      `OnboardingToken`/`Account` fields, not `Website`) — both fall
      outside the constraint's literal text ("write to any Website row").
      Account-level writes elsewhere (contact-info edit, password reset,
      setup-email send) are also not guarded, since the constraint is
      scoped to the Website model specifically, where the subscription id
      columns actually live — flagged here for Zach to confirm that
      scoping is what was intended.
- [x] 7e. Full suite: **1720 tests, 4 failures, 1 skipped, 1716 passing.**
      All 4 failures confirmed pre-existing and unrelated:
      `GmbIntakeFollowupTests` (2) — confirmed via `git stash` against
      the Phase-0-only commit, fails identically with zero build changes
      applied (see Phase 1 notes). `LegacyChainDetectionTests` and
      `PlannedMigrationDependencyTests` (2) — confirmed via
      `git diff main...v2-dashboard --stat -- billing/` returning empty;
      this build never touched `billing/`, so these are pre-existing
      drift in that app (matches the queued "Legacy owner FK cleanup"
      work). The skip is `pyflakes not installed` in this environment,
      also pre-existing. One real failure WAS caused by this build —
      `PublicCssBundleTests.test_bundle_is_not_stale`, because editing
      `main.css` for the v2 CSS section left the generated `public.css`
      bundle stale — fixed by running `manage.py build_public_css` and
      committing the regenerated bundle.
- [x] 7f. `manage.py check --deploy` — 5 pre-existing warnings (HSTS,
      SSL redirect, cookie security, DEBUG), all settings.py-driven and
      unrelated to this build (settings.py was never touched).

## Final report

- [x] `/V2_BUILD_REPORT.md` written.
