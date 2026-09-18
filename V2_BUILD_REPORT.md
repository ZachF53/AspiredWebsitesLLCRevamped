# V2 Dashboard — Build Report

Branch: `v2-dashboard`. Not deployed. Not merged. Everything below is on
this branch only.

---

## 1. Phase 0 findings — what differed from the brief

All five assumptions (0a-0e) held, with two things worth flagging that
weren't fully captured in the original brief:

- **0a (intake status bug)** was confirmed, but it's worse than stated:
  `_on_intake_submitted` also wrote the invalid string `'onboarding_complete'`
  onto `Account.onboarding_status` (valid choices there are only
  `pending_setup`/`complete`), not just `Website.onboarding_status`. Fixed
  both.
- **0e (reconcile_subscriptions)** IS on a daily Celery beat schedule
  (4am), not manual-only. I read the command: it only cancels a hosting
  subscription when the site's droplet is confirmed dead, so it shouldn't
  touch Burgland Tech / Denis Law Group as long as their droplets are up
  — but it's a pre-existing automated write path that reaches the two
  live subscriptions every night regardless of this build. Not modified
  (lives in `billing/`), flagged per the checkpoint instruction.

Full detail with line numbers is in `V2_BUILD_TODO.md` under Phase 0.

---

## 2. Files created

**Shared fix (Phase 1):**
- `clients/management/commands/fix_stuck_onboarding_status.py` — read-only
  report command for Website/Account rows stuck with an invalid
  `onboarding_status`.
- `clients/tests_fix_stuck_onboarding_status.py` — 4 tests for the command.

**v2 app (Phases 2-6):**
- `admin_dashboard/v2/__init__.py`, `urls.py`, `services.py` — package,
  routing, and every query/computation the views use.
- `admin_dashboard/v2/views_toggle.py` — the v1/v2 session-flag toggle.
- `admin_dashboard/v2/views_dashboard.py` — landing page + async money partial.
- `admin_dashboard/v2/views_accounts.py` — accounts list/detail/create,
  setup-email send, password reset.
- `admin_dashboard/v2/views_websites.py` — the core page: list, tabbed
  detail, stage control, payment override, intake reminder, run-scan,
  auto-send toggle, and the live-subscription write guard.
- `admin_dashboard/v2/views_domains.py`, `views_billing.py` — read-only
  aggregate list views.
- `admin_dashboard/templates/admin_dashboard/v2/*.html` — 9 templates
  (dashboard, `_money` partial, accounts_list, account_detail,
  account_create, websites_list, website_detail, domains_list,
  billing_list).
- `admin_dashboard/tests_v2_smoke.py` — 25 tests covering every v2 page,
  every website-detail tab across 4 website states, and every write
  action's guards.

---

## 3. Files modified — what changed and why it was unavoidable

- **`clients/views.py`** — the one shared-code fix the brief called for
  (Phase 1): `_on_intake_submitted` no longer writes an invalid
  `onboarding_status` value. Necessary because this is the bug the whole
  build was partly commissioned to fix, and it lives in `clients/`, not
  `admin_dashboard/`.
- **`clients/tests_intake_submit.py`** — updated one test's assertions
  (`onboarding_complete` → `intake_complete`/`complete`) because it had
  been asserting the bug itself. Unavoidable — the old assertion was
  wrong on its face once the fix landed.
- **`admin_dashboard/navigation.py`** — added `NAVIGATION_V2` and rewrote
  `navigation()` to branch on the session flag. `NAVIGATION` (the v1
  tuple) itself was never touched. Unavoidable — this is the one file the
  brief explicitly designed the toggle around ("navigation.py's context
  processor chooses NAVIGATION_V1 or NAVIGATION_V2").
- **`admin_dashboard/tests_navigation.py`** — extended with
  `NavigationV2DefinitionTests` and `ToggleTests`; zero v1 assertions
  changed. Explicitly authorized by the brief (Phase 2d).
- **`admin_dashboard/urls.py`** — added an include for
  `admin_dashboard.v2.urls`, two toggle routes, and one import line
  (`from django.urls import path` → `from django.urls import include,
  path`, plus a new `from .v2 import views_toggle as v2_toggle` line). No
  existing `path(...)` entry was edited, reordered, or removed — the only
  technically-a-modification here is that one import line grew an added
  name; nothing behavioral changed for any existing route. This is the
  smallest possible touch to wire in an additive include.
- **`core/static/css/main.css`** — appended one clearly marked
  `/* ── v2 dashboard ── */` section at the end (action list, stat tiles,
  tab bar, key/value table, photo grid, onboarding-step row). Nothing
  existing in the file was edited. Unavoidable side effect: this file
  feeds a generated bundle (`public.css`) checked by
  `core.tests.PublicCssBundleTests`; regenerated it via
  `manage.py build_public_css` and committed the result (see §5).

Nothing in `admin_dashboard/views.py`, any `views_*.py`, or any existing
template was touched, per the hard constraint.

---

## 4. TODO list — final state

`V2_BUILD_TODO.md` on this branch has every item checked off, verified by
running the actual page/test rather than by memory. Nothing is
incomplete. One item was blocked-then-fixed rather than clean on the
first pass: 7d (live-subscription write guard) found a real gap in
`website_stage` and `website_toggle_auto_send_scan`, which I fixed and
covered with 3 new regression tests before marking it done — see §8 for
what that gap was.

---

## 5. Test results

**Targeted (Phase 1):** `clients.tests_intake_submit`,
`clients.tests_fix_stuck_onboarding_status`, `clients.tests_portal_gate`
— 18/18 pass.

**Targeted (Phases 2-6):** `admin_dashboard.tests_navigation`,
`admin_dashboard.tests_v2_smoke` — 47/47 pass.

**Full suite** (`python manage.py test`, run twice — once before and
once after regenerating the CSS bundle):

- **1720 tests, 4 failures, 1 skipped, 1716 passing.**
- One failure I caused and fixed: `PublicCssBundleTests.test_bundle_is_not_stale`
  — editing `main.css` left the generated `public.css` stale. Fixed by
  running `manage.py build_public_css` and committing the regenerated
  bundle; confirmed green afterward.
- Four failures are pre-existing and unrelated to this build, verified,
  not just asserted:
  - `GmbIntakeFollowupTests.test_have_sends_add_manager_and_creates_todo`
    and `test_need_sends_create_and_creates_todo` — confirmed via
    `git stash` against the commit containing only Phase 0's TODO file
    (i.e., before any code change in this build existed): both fail
    identically. Root cause (for your awareness, not fixed — out of
    scope): `_on_intake_submitted` tries
    `SiteChangelogEntry.objects.create(website_new=profile, ...)` where
    `profile` is a legacy `ClientProfile` in that test's call shape, but
    `website_new` is FK'd to `Website` — raises `ValueError`, caught by a
    surrounding `except Exception`, but something downstream (GMB
    SetupTodo creation) never runs as a result.
  - `LegacyChainDetectionTests.test_the_repository_is_clean` and
    `PlannedMigrationDependencyTests.test_every_dependency_names_the_current_leaf`
    — confirmed via `git diff main...v2-dashboard --stat -- billing/`
    returning completely empty: this build never touched `billing/`, so
    whatever these tests are catching (a legacy `.client` read in
    `billing/webhooks.py`, and a `.planned` migration file pointing at a
    stale dependency) predates this branch entirely. Matches the
    already-known "Legacy owner FK cleanup" queued work.
- `manage.py check` — clean, 0 issues.
- `manage.py check --deploy` — 5 pre-existing warnings (HSTS, SSL
  redirect, session/CSRF cookie security, DEBUG=True), all controlled by
  `settings.py`/`.env`, which this build never touched — these are
  dev-environment warnings that would be identical on `main`.

---

## 6. Decisions made that weren't specified in the prompt

- **Mount point**: the brief's `/dashboard/v2/`, `/dashboard/use-v2/`,
  `/dashboard/use-v1/` are realized as `/admin-dashboard/v2/`,
  `/admin-dashboard/use-v2/`, `/admin-dashboard/use-v1/` — the app's real
  mount point (set before this build, in the project root urls.py) is
  `/admin-dashboard/`, not `/dashboard/`. Read `/dashboard/...` as
  shorthand for the existing prefix rather than a literal new one.
- **Money block reads local data, not live Stripe**: the brief said "read
  live from Stripe." I used `clients.revenue.get_current_mrr()` (existing,
  local-DB MRR calculation) and a local `PaymentRecord` sum for cash
  collected, instead of writing new Stripe API read calls. Reasoning:
  hard constraint #2 says "no `stripe.*` call anywhere," and I judged a
  brand-new Stripe API integration — even read-only — as more risk than
  the brief's wording was worth, especially for an overnight,
  unsupervised run. The existing `get_current_mrr()` is also the function
  CLAUDE.md-style conventions on this project would want reused anyway.
  Still async-loaded via HTMX and 5-minute-cached, per the brief's
  performance requirement.
- **Password reset reimplemented, not called**: v1's
  `account_send_password_reset` is a full view (its own redirect,
  messages, template context) rather than a callable service function.
  Calling it directly from v2 would bounce the admin into v1's page after
  the reset email sends. I reimplemented the same 4 lines
  (`PasswordResetForm` + the same two template names v1 uses) inside a v2
  view so the redirect stays in v2. Same mechanism, same email templates
  — not a new email, just a new caller.
- **Off-Stripe payment override reuses `verify_website_payment` via
  `call_command`**, discovered mid-build — I hadn't known this management
  command existed until researching the payment-evidence guard. Using it
  (rather than hand-writing the same three field assignments) means the
  exact same audited attestation pattern the CLI already uses.
- **Infrastructure tab links out instead of rebuilding power controls**:
  rather than reimplementing droplet power-on/off in v2, the tab shows
  read-only droplet info and links to v1's existing Droplet
  metrics/power page, the Deploy runbook, and Vault Ops Sessions.
  Reasoning: power controls can take a live client's site offline, and
  duplicating that logic in a second, less-tested UI seemed like
  unnecessary risk for a feature the brief didn't explicitly ask me to
  rebuild ("deploy runbook link, ops session launcher" reads as links,
  not a reimplementation).
- **Domains tab/list are read-only with a link out** for the same reason
  applied to DNS/repoint — the brief explicitly says "No transfer-out, no
  delete in v2 yet" for the Domains list page (6a); I extended that same
  restraint to the per-website Domains tab.
- **`fix_stuck_onboarding_status` has no `--apply` path at all**, not
  just a `--dry-run`-by-default one. The brief said "Do NOT auto-fix" —
  I read that as "this command never writes," not "writes only behind a
  flag," since a stuck row's correct target value needs a human to
  actually look at the client's real state before deciding.

---

## 7. "Coming soon" disabled controls, and what each needs

All of these are disabled buttons with a `title="Coming soon"` tooltip;
none call Stripe or any billing mutation.

- **Website → Onboarding tab, "Resend contract"** — no existing
  standalone "resend contract" email function was found. Needs: either a
  new `clients.emails` function, or confirmation that
  `send_contract_signed_email`'s sibling for an unsigned/pending contract
  already exists somewhere I didn't find.
- **Website → Onboarding tab, "Send payment reminder"** — same gap; no
  existing reusable payment-reminder-for-a-pending-deposit function was
  found (the dunning email sequence is for *failed* payments post-
  activation, not a first-invoice nudge).
- **Website → Billing tab, "Change plan" / "Cancel subscription"** — per
  the brief, any Stripe-mutating action stays disabled in this build.
  Needs the actual plan-change/cancel flow, which lives in `billing/` and
  is explicitly off-limits here.

---

## 8. Found but out of scope

- **`GmbIntakeFollowupTests` failure** (§5) — a real, pre-existing bug in
  `_on_intake_submitted`'s changelog-entry creation for the legacy
  `ClientProfile` call shape. Unrelated to the `onboarding_status` bug
  this build targeted; left alone.
- **`LegacyChainDetectionTests` / `PlannedMigrationDependencyTests`
  failures** (§5) — pre-existing drift in `billing/`, matches the already
  -tracked "Legacy owner FK cleanup" queued work. Not touched, per the
  Stripe/billing hard constraint anyway.
- **`_on_intake_submitted` also had a second, unbriefed bug**: it wrote
  the same invalid string onto `Account.onboarding_status`, not just
  `Website.onboarding_status`. Fixed as part of Phase 1 since it's the
  same root cause, same file, same fix shape (see §3).
- **No SSL certificate expiry tracking exists anywhere** in the codebase
  — confirmed by a repo-wide grep, not an assumption. The landing
  dashboard's action list skips that source entirely, per the brief's own
  "skip if not [available]" instruction.
- **No session-recording viewer exists** for `Website.session_recording_enabled`
  — the Monitoring tab shows the boolean status only; there's nothing to
  link to.

---

## 9. What to check first in the morning

1. **The live-subscription write guard** (§10, "shaky" list below) — I'm
   fairly confident in it (3 passing regression tests), but it's new code
   protecting real client data, so it's worth a second pair of eyes
   before anyone relies on it.
2. **The off-Stripe payment override** (Website → Overview tab) — this is
   the one control in this build that changes `payment_status` and moves
   a site to `live`. It's gated (reason required, guarded against
   live-subscription sites, reuses the existing attestation command), but
   it's also the highest-consequence action I built. Test it against a
   throwaway local Website before trusting it on anything real.
3. **The Intake tab** — this was explicitly the thing you said you most
   needed. Load it against a real client's submitted intake locally and
   confirm every field and photo you expect actually shows up; my test
   fixtures used minimal/empty `IntakeResponse` rows, so field-by-field
   correctness against real data hasn't been eyeballed by a human yet.
4. Toggle into v2 (`/admin-dashboard/use-v2/`) and click through all six
   nav items once, then click into Vault and confirm the v2 sidebar
   followed you there.

---

## 10. Honest assessment

**Solid:**
- The Phase 1 fix and its command — small, well-tested (18 tests), and I
  traced the exact aliasing bug rather than guessing at a fix.
- The toggle mechanism — this was the part of the brief I was most
  worried about getting subtly wrong (base.html edits, namespace
  collisions), and it came out clean: no v1 template touched, verified
  the flag survives into Vault, all 23 navigation tests green including
  the original v1 ones, unmodified.
- The website-detail tabs render correctly across four different website
  states (brand new / live / wordpress / archived) — verified by test,
  not assumed.
- The live-subscription guard — I initially missed it, but I found it
  myself during the Phase 7d self-check (not because a test caught it
  first) and fixed it before calling the phase done, which is closer to
  the outcome you'd want than either missing it entirely or having it
  slip through to review.

**Shaky:**
- The Intake tab has never been looked at against a real client's actual
  submitted answers — only synthetic/empty test fixtures. Given you said
  this was the feature you most needed, it deserves a real look before
  you rely on it.
- The account-level write paths (contact-info edit, password reset,
  setup-email send) are NOT guarded against the two live-subscription
  clients, only Website-row writes are. I made a judgment call that the
  hard constraint's literal wording ("Website row") doesn't cover
  Account edits, but if your intent was broader — protect Burgland Tech
  and Denis Law Group's records generally, not just the Website row —
  this needs tightening. I flagged this explicitly rather than guessing
  wrong silently.
- The "30+ days outstanding" invoice logic is anchored on `sent_at`, not
  a due date, because no due-date field exists on `OnboardingInvoice`
  anywhere in the codebase. It's honestly labeled as such in the code
  comment, but the number it produces answers a slightly different
  question than "30 days overdue."

**What I'd redo:**
- I'd build a small, dedicated fixture with a fully-populated
  `IntakeResponse` (real-shaped brand colors, bios, photos) before
  calling the Intake tab done, rather than relying on the smoke tests'
  minimal fixtures — that's the one place "renders without crashing" and
  "actually useful" aren't the same claim, and I only verified the
  former.
