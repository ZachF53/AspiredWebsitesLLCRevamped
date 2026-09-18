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

## Phase 2 — v2 scaffolding

- [ ] 2a. `admin_dashboard/v2/` package: views, urls, templates dir.
      Included under `/dashboard/v2/`. Uses existing `admin_required`.
- [ ] 2b. Toggle: session key `dashboard_version`, routes
      `/dashboard/use-v2/` and `/dashboard/use-v1/`. navigation.py context
      processor branches on it.
- [ ] 2c. `NAVIGATION_V2`: Dashboard, Accounts, Websites, Domains, Billing, Vault.
- [ ] 2d. Extend tests_navigation.py for NAVIGATION_V2 without touching v1 assertions.
- [ ] 2e. v2 templates extend v1's base/layout; any new CSS goes in a marked
      v2 section at the end of core/static/css/main.css.

## Phase 3 — Landing dashboard (`/dashboard/v2/`)

- [ ] 3a. Merged, urgency-sorted action list (dunning, alerts, scans,
      SSL expiry if available, site_status mismatch, invoices 30+ days,
      pending setup, pending intake, needs_admin_review_at).
- [ ] 3b. Counts block.
- [ ] 3c. Money block (MRR + cash collected), async, 5-min cache, read-only,
      never blocks render.
- [ ] 3d. `admin_dashboard/v2/services.py` holds querysets/computations.

## Phase 4 — Accounts

- [ ] 4a. List page.
- [ ] 4b. Detail page: contact/billing info, websites, setup-email button
      w/ 24h cooldown, password reset.
- [ ] 4c. Create-account (records only, no email).

## Phase 5 — Websites (core)

- [ ] 5a. List page.
- [ ] 5b. Detail page tabs: Overview, Onboarding, Intake, Infrastructure,
      Security, Domains, Billing, Monitoring.
- [ ] 5c. Every tab renders for any website state without crashing.

## Phase 6 — Domains / Billing list pages

- [ ] 6a. `/dashboard/v2/domains/`
- [ ] 6b. `/dashboard/v2/billing/`

## Phase 7 — Verification

- [ ] 7a. Re-walk this list, verify by exercising, not memory.
- [ ] 7b. v1 unchanged — full v1 test suite, nav renders, toggle works.
- [ ] 7c. Grep diff for `stripe.` calls — must be zero new ones.
- [ ] 7d. Confirm no write path reaches a Website with a non-null subscription id.
- [ ] 7e. Full test suite — honest pass/fail.
- [ ] 7f. `manage.py check --deploy`

## Final report

- [ ] Write `/V2_BUILD_REPORT.md`
