# Legacy Owner FK — Codebase Cleanup Handoff

**Status:** open. Twelve instances found and fixed 2026-08-26/27 by walking
the client onboarding flow end to end. They were found one crash at a time.
This document exists so the rest can be found in one deliberate pass instead.

**Scope of this doc:** one recurring bug class (§1–§6), plus a second,
smaller one that shares the "fails silently" property (§7).

---

## 1. The bug class in one paragraph

The codebase migrated from a single `ClientProfile` model to a canonical
`Account` (the billing/login entity) + `Website` (the thing being built).
`ClientProfile`, `Project`, and the `client` / `project` foreign keys that
point at them are **legacy** and are **null on every record created since
the refactor**. Large amounts of code still reach through those FKs
(`contract.client.firm_name`, `intake.project.client_id`,
`profile.user`), or assign a canonical object to a legacy FK
(`PaymentRecord.client = <Account>`). On a post-refactor record this
raises `AttributeError` or `ValueError` — and in most cases the caller
catches it, logs it, and carries on.

---

## 2. Why it stays hidden

This is the important part. The failures are not loud.

| Mechanism | Effect |
|---|---|
| **Best-effort `except`** around emails, ledger writes, file copies, provisioning | The exception is logged and swallowed. The user-facing action "succeeds". |
| **`getattr(obj, 'x', None)`** | Django's `RelatedObjectDoesNotExist` subclasses `AttributeError`, so a missing relation returns the default and reads as "nothing here" rather than "wrong relation". |
| **`filter(client=None)`** | Matches *every* row whose legacy FK is unset — i.e. some other account's record — instead of matching nothing. Silent cross-account data corruption. |
| **`send_mail(recipient_list=[])`** | A no-op. Django does not complain about mailing nobody. |
| **`OneToOneField(null=True)`** | Multiple NULLs are allowed, so a "1:1" constraint quietly stops constraining anything once the FK goes null. |
| **htmx / fetch error responses** | A 500 from an `hx-post` is discarded; the UI simply does nothing. |

Consequence: **the money moves, the page returns 200, and the record
vanishes.** Nothing reaches the operator. Every one of the twelve below
was found by a human noticing a missing email, a blank page, or an empty
list — never by an error report.

---

## 3. Where each field actually lives

Verified against the models on 2026-08-27. This table is the fix for most
occurrences — the attribute usually exists, just on a different object.

### `Account` (billing + login owner)
```
user, name, contact_name, phone, email_alt, address, city, state, zip_code,
country, status, is_tester, internal_notes, stripe_customer_id,
stripe_social_subscription_id, onboarding_status, onboarding_complete,
client_pin_hash, client_pin_salt, client_pin_set,
client_pin_failed_attempts, client_pin_lockout_until,
comp_build_package, comp_maintenance_package, comp_social_tier,
payment_failure_started_at, payment_failure_offenses
```

### `Website` (one build / one site)
```
account, name, slug, business_type, url, staging_url, status, stage,
package, build_platform, custom_build_price, onboarding_status,
lifecycle_status, opted_in_maintenance_tier, opted_in_social_tier,
do_droplet_id, do_droplet_ip, do_droplet_name, do_snapshot_id, site_status,
launch_date, support_window_ends, payment_status, deposit_paid_at,
final_paid_at, needs_admin_review_at, admin_reviewed_at,
revision_count, revision_limit, stripe_hosting_subscription_id,
stripe_maintenance_subscription_id, stripe_invoice_id, final_invoice_url,
moonieful_*
```

### Traps in that table
- `onboarding_status` exists on **both** — they are not the same thing.
- `onboarding_complete` is **Account only**. Writing it on a Website
  raises `ValueError` from `save(update_fields=...)`.
- `needs_admin_review_at` / `admin_reviewed_at` are **Website only**.
- `user` is **Account only**. A Website reaches it via `website.account.user`.
- `firm_name` is **ClientProfile only**. Account calls it `name`.
- The Stripe **subscription** id for maintenance/social lives on
  `MaintenancePlan` / `SocialMediaPlan`, **not** on Account.
- The Stripe **customer** id lives on Account.

---

## 4. The twelve already fixed — use these as the pattern

| # | Location | Symptom | Fix | Commit |
|---|---|---|---|---|
| 1 | `clients/emails.py` `send_contract_ready_email`, `send_contract_signed_email` | Contract signing link emailed **to nobody**; operator saw "Contract sent" | `_contract_owner()` → `contract.client or contract.account` | `83967f7` |
| 2 | `clients/views.py` `contract_sign` | 500 on POST — `contract.client.package` | Guard the legacy mirror with `if client is not None` | `83967f7` |
| 3 | `billing/stripe_helpers.py` `start_contract_payment` | Payment couldn't start; client bounced to a generic page with no way to pay. Also `filter(client=None)` overwrote **another account's** invoice | Resolve `owner = client or account`; look up by website/account | `393b929` |
| 4 | `billing/views.py` `pay_success` | "Thank you, ." and no setup link — looked for `onboarding_token` (ClientProfile-only relation) | `_invoice_owner()`, `_setup_token()` checking both relations | `143e4b5` |
| 5 | `clients/views.py` `onboarding_setup` | Would 500 — `onboarding_token.client.user` | `client or account_new` | `143e4b5` |
| 6 | `billing/webhooks.py` setup-link resend | Never fired — wrong relation name on Account | Check both; mint the token if missing | `143e4b5` |
| 7 | `clients/models.py` `intake_photo_path` | Photo upload silently did nothing — `intake.project.client_id` | Key on `website_new` | `101f58b` |
| 8 | `clients/views.py` `_on_intake_submitted` | 500 **after** intake saved; portal stayed locked; provisioning/email/changelog all skipped | Write `onboarding_complete` on the Account | `101f58b` |
| 9 | `clients/views.py` `_copy_intake_files_to_documents`, GMB `SetupTodo` | Logos/photos never reached Files page; GBP task never created — `profile.user` | `profile.account.user` | `19b50f3` |
| 10 | `billing/stripe_helpers.py` `start_contract_final_payment` | Pre-Launch raised **no final invoice**, no email, no portal Pay button — while the launch gate blocked on it | Same owner resolution | `f73f1dc` |
| 11 | `clients/emails.py` `_display_name` + `OnboardingInvoice` 1:1 | Emails greeted the **site name**; final payment **overwrote the paid deposit** row | Reach through to Account; both FKs → `ForeignKey` (migration `0060`) | `3ce89fb` |
| 12 | `billing/webhooks.py` `_record_payment` | **No payment ever written to the ledger** for an account — Invoices page permanently empty, no receipts | Assign profile only when it is one; derive account | `3d52e33` |

---

## 5. How to find the rest

Run these from the Django project root. Exclude `migrations/` and test
files on the first pass; audit them separately.

```bash
# Reaching through the legacy owner FKs
rg -n '\.client\.' --type py | rg -v 'migrations/|tests?_|/tests\.py'
rg -n '\.project\.' --type py | rg -v 'migrations/|tests?_|/tests\.py'

# Legacy-only attribute names on something that may be canonical
rg -n '\.firm_name' --type py --type html
rg -n '\bprofile\.user\b|\bwebsite\.user\b|\.user_id\b' --type py

# Assigning into a legacy FK (the ValueError shape)
rg -n "client\s*=\s*(account|owner|profile|request\.account)" --type py

# Lookups that match every null row — cross-account corruption risk
rg -n 'filter\(\s*client\s*=' --type py
rg -n 'filter\(\s*project\s*=' --type py

# Relations that exist on only one side
rg -n 'onboarding_token\b(?!_new)' --type py
rg -n 'onboarding_invoice\b(?!s)' --type py --type html

# Where failures get buried — cross-reference hits above against these
rg -n -B2 'except Exception' --type py | rg -n 'logger\.(exception|warning)'
```

**Prioritise by blast radius, not by count:** anything inside a Stripe
webhook, an email send, or a `try/except` that wraps a customer-facing
action. Those are the ones that have already been failing in production
without anyone knowing.

---

## 6. Fix recipes

Helpers that already exist — reuse rather than re-inventing:

| Need | Use |
|---|---|
| Display name for any owner | `clients.display.owner_label` / `clients.emails._display_name` |
| Email recipient for any owner | `clients.display.owner_recipient` / `clients.emails._recipient` |
| Account behind any row | `clients.display.owner_account` |
| Contract's owner | `clients.emails._contract_owner` |
| Invoice's owner | `billing.views._invoice_owner` |
| Invoice's setup token | `billing.views._setup_token` |
| Maintenance subscription id | `billing.stripe_helpers._maintenance_sub_id` |
| Plan row for a subscription | `billing.account_provisioning._plan_row_for_subscription` |

**Canonical resolution order** — prefer the canonical object, fall back to
legacy, never assume either exists:

```python
owner = row.client or row.account          # ClientProfile → Account
website = row.website_new or getattr(row.project, 'website', None)
user = getattr(owner, 'user', None)
name = (getattr(owner, 'firm_name', '')       # legacy
        or getattr(owner, 'name', ''))        # canonical
```

**Rules for the cleanup pass**

1. **Never** assign a canonical object to a legacy FK. Set it to `None`
   and populate the canonical column instead (see #12).
2. **Never** `filter(<legacy_fk>=<possibly None>)`. Key on
   `website_new` / `account_new`, or add `.exclude(<fk>__isnull=True)`.
3. When a `try/except` hides a customer-facing side effect, make the
   failure **visible** — a `logger.error` with the object id at minimum,
   an operator-facing message where one exists (see the `website_send_contract`
   "NOT emailed" warning added in `83967f7`).
4. `save(update_fields=[...])` is a landmine: it raises for any field the
   model doesn't have. Any field-name change needs the update_fields list
   checked too.
5. Don't "fix" by adding the missing field to the canonical model. The
   field almost always already exists somewhere correct — check §3 first.

---

## 7. Second class: inline `<script>` under CSP

Not the same root cause, but the same silence.

`/admin-dashboard/` serves `CSP_ADMIN_DASHBOARD`, which inherits
`script-src 'self'` from `CSP_PUBLIC` — **no `'unsafe-inline'`**. Any
inline `<script>` block on those pages **never executes**. The browser
blocks it, the page still returns 200, and the feature just doesn't work.

Fixed once already: the custom-amount field on the new-invoice form was
unreachable for this reason (`2423394`). **Six admin templates still
carry inline blocks** and are very likely dead the same way:

```
_proposal_autofill.html   case_study_form.html    proposal_detail.html
proposal_new.html         recording_download.html referrals_list.html
```

Find them with:
```bash
# `<script>` ending a line = an inline block. Matching bare `<script>`
# anywhere also hits the word inside {% comment %} prose — verified.
rg -l '<script>\s*$' admin_dashboard/templates/
```

`website_detail.html` and `billing_new_invoice.html` match a looser grep
but only inside `{% comment %}` prose describing this very rule — both
already load external files correctly. Confirm per file before editing.

Note inline blocks are only *dead* under the admin-dashboard and public
CSPs. `/admin/` (Django admin) uses `CSP_ADMIN`, which **does** allow
`'unsafe-inline'`, and the SSH terminal page has its own policy — check
which CSP a template is served under (`core/middleware.py`) before
assuming it is broken.

**Fix:** move the JS to `core/static/js/<name>.js` and load it with
`{% static %}` + `?v={{ STATIC_VERSION }}` (the cache-buster is required —
`/static/` is immutable for 30 days). Match `sidebar.js` / `input_masks.js`.
A regression test asserting the page carries **no** inline block is in
`admin_dashboard/tests_invoice_custom_amount.py` — copy it.

---

## 8. Verifying the cleanup

**Prod audit for damage already done** (read-only — run before fixing):

```python
# Paid invoices with no ledger row — the #12 blast radius
from clients.models import OnboardingInvoice, PaymentRecord
paid = OnboardingInvoice.objects.filter(status='paid')
missing = [i for i in paid
           if not PaymentRecord.objects.filter(
               stripe_id=i.stripe_payment_intent_id).exists()]
print(len(missing), 'paid invoices missing a PaymentRecord')

# Plan rows orphaned from their website — the duplicate-plan shape
from clients.service_models import MaintenancePlan
print(MaintenancePlan.objects.filter(website__isnull=True)
      .exclude(stripe_subscription_id='').count())

# Accounts whose contract email could never have been delivered
from clients.models import Contract
from clients.emails import _contract_owner, _recipient
for c in Contract.objects.filter(signed=False):
    if not _recipient(_contract_owner(c)):
        print('unreachable:', c.pk)
```

**Testing.** Per `CLAUDE.md`, run the specific test module, not whole
apps. Existing regression modules to extend rather than duplicate:

```
billing/tests_contract_payment_start.py    billing/tests_final_payment.py
billing/tests_post_payment_setup.py        billing/tests_payment_ledger.py
billing/tests_plan_dedup.py                billing/tests_maintenance_cancel.py
clients/tests_contract_account_owner.py    clients/tests_intake_submit.py
clients/tests_contract_sign_page.py
admin_dashboard/tests_custom_build_contract.py
admin_dashboard/tests_invoice_custom_amount.py
```

Each opens with a docstring stating the symptom and why it hid. Keep that
convention — it is what makes these findable later.

**A test that would have caught most of this:** build an Account + Website
with **no** `ClientProfile` and walk the whole flow. Every one of the
twelve fails on that fixture. Worth adding as an integration test.

---

## 9. Deploy notes

- Two servers: staging `167.99.154.2`, prod `161.35.108.209`. Deploy
  staging first when there's a migration.
- `collectstatic` on every deploy that touches `static/`.
- `main.css` is hand-edited; `public.css` is **derived** — run
  `python manage.py build_public_css` after any `main.css` change or its
  staleness check fails.
- A new `clients` migration displaces the leaf that
  `clients/migrations_planned/*.planned` depends on. Re-point it or
  `clients.tests_planned_migrations` fails.

---

*Written 2026-08-27 after the twelve fixes above shipped to staging and
production.*
