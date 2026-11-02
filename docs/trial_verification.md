# Free trial: verification checklist and hand steps

Everything below is **Stripe TEST mode**. No step here touches live
keys or live money.

## Deployment record

Shipped to **staging and production** on 2026-11-02 at commit `8b13a8a`,
merged to `main` as a fast-forward (no history rewritten).

| | Done |
|---|---|
| Migrations | `billing.0016`, `0017`, `0018`, `voice.0005` applied on both |
| `collectstatic` | run on both (CSS and JS both changed) |
| Env vars | `TRIAL_DAYS`, `TRIAL_REMINDER_DAYS`, `TERMS_VERSION`, `BILLING_SUPPORT_EMAIL` set on both. `.env` backed up first on each |
| Services | gunicorn + celery restarted on both. celerybeat restarted on prod; **left stopped on staging** by design |
| GHL custom fields | created, and their ids pinned in settings (commit `64f4556`) |
| Stripe live webhook | all seven required events already enabled on `aspiredwebsites.com/billing/webhook/`, verified by API read |
| Stripe live prices | both `$97/mo` Price objects already existed on the two tiers, so checkout works |

Verified live after deploy: `/terms/` shows version 2026-11-01, the
Atlanta entity address, Fulton County venue and `billing@`; both
checkout pages show the trial CTA, the terms checkbox, the disclosure
and `$0.00` due today; home, pricing, both service pages and contact
all 200.

**The two $97/mo products now take no payment for 14 days.** Anyone who
signs up is charged on day 15.

One log line worth knowing is benign: `core.cache_health` logs an ERROR
at startup when several gunicorn workers write the same probe key at
once and one reads back another's value. The cache round-trips fine.
Pre-existing, unrelated to this work.

---

Branch merged to `main`. The old `stripe-trial-and-terms` branch was
deleted after confirming it was fully contained in what shipped.

---

## Part 1. Hand steps you have to do first

Nothing in Part 2 works until these are done. Items 1 and 3 are the two
that change customer-visible behaviour.

### 1.1 Stripe Dashboard (test mode)

| # | Where | What | Why |
|---|---|---|---|
| 1 | Products | Confirm both recurring **$97/month** prices exist in test mode | |
| 2 | Admin dashboard → `/admin-dashboard/pricing/` | Paste each test price id onto its `ServiceTier` row (`hvac-voice-receptionist`, `hvac-review-system`), or run `python manage.py sync_stripe_products` to create and write them back automatically | **Not an env var.** `ServiceTier.stripe_price_id` is the source of truth, and the receptionist price moves up a rung every 10 accounts via `voice/cohorts.py`. An env var would pin new signups to the opening price. |
| 3 | Developers → Webhooks → your endpoint | Confirm these events are enabled: `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.trial_will_end`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`, `invoice.created` | `trial_will_end` and `subscription.updated` are the new ones. `invoice.created` was already required for voice overage billing (CLAUDE.md) and still is. |

### What you do NOT have to configure

Two Stripe Dashboard settings an earlier draft of this document asked
for are no longer needed, because the application does both jobs
itself:

- **"Send emails about upcoming trial expirations"** — leave it OFF. We
  send our own pre-charge reminder from `DEFAULT_FROM_EMAIL`, in the
  same branded template as every other email, carrying our own cancel
  link. See `billing/trial_reminders.py`. Relying on a dashboard
  checkbox was the worse design: nothing in the code could verify it
  was ticked, and the failure mode was a customer charged with no
  warning.
- **Customer portal** — leave it off entirely. There is no link into
  Stripe's hosted portal anywhere on this site. Cancelling happens at
  `/billing/trial/<slug>/manage/` for a trial and in the client portal
  after that. A test asserts nothing on the manage page points at
  stripe.com.

Terms of Service and Privacy URLs under Business → Public details are
optional now too: they were only needed for Stripe-hosted Checkout's
`consent_collection` and for the hosted portal, and we use neither.
Setting them is still reasonable, since they show on Stripe receipts.

### 1.2 GoHighLevel custom fields

Create these four by hand in the sub-account. Nothing creates them from
code, and the code tolerates their absence (the tag still lands, the
fields are silently dropped by GHL).

| Field key | Type |
|---|---|
| `trial_start` | Date |
| `trial_end` | Date |
| `stripe_customer_id` | Text |
| `stripe_subscription_id` | Text |

Keys must be **bare** (`trial_start`, not `contact.trial_start`).

Optionally add the field ids to `GHL_CUSTOM_FIELD_IDS` in `settings.py`
afterwards; `outreach/ghl.py` falls back to the bare key when an id is
absent, which is what it does today.

You will also want GHL workflows keyed on the new tags:

```
trial-started-hvac-voice-receptionist     trial-started-hvac-review-system
trial-ending-hvac-voice-receptionist      trial-ending-hvac-review-system
paid-hvac-voice-receptionist              paid-hvac-review-system
cancelled-hvac-voice-receptionist         cancelled-hvac-review-system
payment-failed-hvac-voice-receptionist    payment-failed-hvac-review-system
```

### 1.3 Environment variables

All three environments. Every one has a working default, so nothing
breaks if you skip them, but set them so the environments agree.

```bash
TRIAL_DAYS=14
TRIAL_REMINDER_DAYS=3
TERMS_VERSION=2026-11-01
BILLING_SUPPORT_EMAIL=zacherylong@aspiredwebsites.com
```

| Environment | `.env` path |
|---|---|
| Local (Windows) | `AspiredWebsitesRevamped\.env` |
| Staging `167.99.154.2` | `/var/www/aspired/app/.env` |
| Prod `161.35.108.209` | `/var/www/aspired/app/.env` |

`.env` never arrives via `git pull`. `TRIAL_REMINDER_DAYS` IS read by
Celery (the daily reminder sweep), so changing that one needs
`supervisorctl restart aspiredwebsites aspiredwebsites-celery
aspiredwebsites-celerybeat`. The others only need gunicorn.

**Staging runs with celerybeat stopped**, so the reminder sweep will not
fire there on its own. Run `python manage.py send_trial_reminders` by
hand to test it.

### 1.4 Still outstanding

- **Confirm `billing@aspiredwebsites.com` receives mail.** It is set on
  both servers and now renders in terms section 21, on the checkout
  disclosure and on the trial manage page, all three of which promise
  cancellation by email. If that mailbox or alias does not exist yet,
  create it. This is the one open item that a customer could hit.
- **Attorney-review markers.** Terms sections 15 and 18 still carry
  `<!-- TODO: attorney review -->` in the HTML. They are invisible to
  visitors. Zachery reported the review itself as done on 2026-11-01;
  the markers were left in place rather than removed on an ambiguous
  "done", since deleting a legal-review marker is not a thing to do by
  inference. Say the word and they come out.

Closed: onboarding copy (written 2026-11-02, two steps answered from
the seeded wizard in `onboarding/registry.py`), the receptionist
turnaround time (2 to 5 business days), and the four GHL custom
fields.

---

## Part 2. Local webhook testing with the Stripe CLI

### Setup

```bash
stripe login
```

Forward events to the local dev server. Leave this running in its own
terminal:

```bash
stripe listen --forward-to localhost:8000/billing/webhook/ \
  --events customer.subscription.created,customer.subscription.trial_will_end,customer.subscription.deleted,invoice.paid,invoice.payment_failed,invoice.created
```

`stripe listen` prints a signing secret (`whsec_...`) on startup. Put it
in your local `.env` as `STRIPE_WEBHOOK_SECRET` and restart
`runserver`, otherwise every forwarded event is rejected by
`_verify_event`.

### Triggering each event

`stripe trigger` builds its own fixture objects, so the events it sends
carry no `trial_signup` metadata and no matching `TermsAcceptance` row.
They verify that the endpoint accepts, routes and does not crash on each
event type. They do **not** exercise the GHL push, which correctly
no-ops without an acceptance row. For the GHL path, use Part 3.

```bash
stripe trigger customer.subscription.created
stripe trigger customer.subscription.trial_will_end
stripe trigger customer.subscription.deleted
stripe trigger invoice.paid
stripe trigger invoice.payment_failed
```

Expect `200` for each in the `stripe listen` output, and a row per event
in Django admin → Billing → **Stripe webhook events**.

### Proving the idempotency gate

Trigger any event, copy its `evt_...` id from the `stripe listen`
output, then resend it from the Dashboard (Developers → Events → the
event → **Resend**). The second delivery logs:

```
Stripe webhook: <type> <evt_id> already processed, skipping
```

and creates no second ledger row.

To re-run an event deliberately (for example after fixing a handler
that errored), delete its row in Django admin → Billing → Stripe
webhook events, then Resend. That is the only reason delete is enabled
on that admin.

---

## Part 3. End-to-end trial, per product

Do this twice: once at
`/billing/checkout/hvac-voice-receptionist/` and once at
`/billing/checkout/hvac-review-system/`.

Note the slug is `hvac-review-system`, not `hvac-review-automation`.

### 3.1 Before entering a card

- [ ] The button reads **"Start 14-Day Free Trial"** and is **disabled**.
- [ ] Above it: "14 days free. Your card will not be charged until
      *&lt;date&gt;*. Then $97/mo, cancel any time..." and the date is
      14 days from today.
- [ ] The Terms checkbox is present. Ticking it enables the button.
      Un-ticking disables it again.
- [ ] The Terms link opens `/terms/` in a new tab, and that page shows
      **"Version 2026-11-01"** at the top.
- [ ] Order summary shows **Today $0.00** and a second line
      "From *&lt;date&gt;*  $97/mo".

### 3.2 Server-side terms gate

With the box unticked, force a submit from the browser console:

```js
document.getElementById('terms-accept').checked = false;
document.getElementById('checkout-submit').disabled = false;
document.getElementById('checkout-form')
        .dispatchEvent(new Event('submit', {cancelable: true}));
```

- [ ] The page shows "Please accept the Terms of Service to continue."
- [ ] No `TermsAcceptance` row was created.
- [ ] No Stripe customer was created.

### 3.3 Start the trial

Card `4242 4242 4242 4242`, any future expiry, any CVC, any postcode.

- [ ] You land on `/billing/trial-started/<slug>/?ref=<uuid>`.
- [ ] It names the product, the trial end date, "$97/mo", and a cancel
      link.

### 3.4 Check the records

Django admin → Billing → **Terms acceptances**:

- [ ] One row. Email, `product_slug`, `terms_version` = `2026-11-01`,
      `accepted_at` set, `ip_address` populated.
- [ ] `stripe_customer_id` and `stripe_subscription_id` filled.
- [ ] Fields are read-only (no Save button).

Stripe Dashboard → Customers → the customer:

- [ ] Subscription status is **Trialing**.
- [ ] Trial end is **14 days out**.
- [ ] A **SetupIntent** exists with status `succeeded`.
- [ ] The subscription has a default payment method.
- [ ] Subscription metadata carries `product_slug`, `terms_version`,
      `terms_accepted_at`, `terms_accepted_ip`, `site_lead_id`,
      `trial_signup=1`.
- [ ] `site_lead_id` equals the `TermsAcceptance` UUID.

GoHighLevel → the contact:

- [ ] Tag `trial-started-<slug>` present.
- [ ] `trial_start` and `trial_end` set as dates, `stripe_customer_id`
      and `stripe_subscription_id` set as text.
- [ ] The contact's **other tags are intact** (test this properly: tag
      the contact `client` and `hvac` by hand first, then run a second
      signup on that same email and confirm both survive).

### 3.5 Voice receptionist only: the cohort did not move

- [ ] `/pricing/` scarcity bar shows the **same** "N of 10 spots left"
      as before the trial started.
- [ ] Django admin → Voice → the program: `cohort` is set,
      `locked_price` is 97.00, `slot_consumed` is **False**.

### 3.6 3DS card

Repeat 3.3 with `4000 0027 6000 3184`.

- [ ] A 3DS challenge appears.
- [ ] After completing it, you reach the trial-started page.
- [ ] Exactly one subscription exists for that customer (not two).

Then repeat with the challenge **cancelled**:

- [ ] An error is shown.
- [ ] **No subscription** was created.
- [ ] The `TermsAcceptance` row exists with an empty
      `stripe_subscription_id`. That is correct: it records that someone
      accepted the terms and did not complete.

---

## Part 4. Lifecycle events

### 4.1 Trial ending

Stripe fires `trial_will_end` three days out, which you will not wait
for. Force it by editing the subscription's trial end:

```bash
stripe subscriptions update sub_XXXX --trial-end=$(date -d '+1 hour' +%s)
```

Or from the Dashboard: subscription → Actions → Update trial end.

- [ ] GHL contact gains `trial-ending-<slug>`.
- [ ] `TermsAcceptance.trial_end` has moved to match, and because the
      date moved LATER, `reminder_sent_at` was cleared so a fresh
      reminder goes out quoting the new date.

### 4.1b Our reminder email

This is the pre-charge reminder, and it is ours rather than Stripe's.
Point a trial at the reminder window and run the sweep:

```bash
python manage.py shell -c "
from billing.terms_models import TermsAcceptance
from django.utils import timezone
from datetime import timedelta
row = TermsAcceptance.objects.latest('accepted_at')
row.trial_end = timezone.now() + timedelta(days=2)
row.reminder_sent_at = None
row.save()
print('due:', row.email, row.trial_end)
"

python manage.py send_trial_reminders --dry-run   # who is due
python manage.py send_trial_reminders             # actually send
```

- [ ] The email arrives from **your** address, in the site's branded
      template, not from Stripe.
- [ ] It states `$97/mo` and the exact charge date.
- [ ] It says nothing has been charged yet.
- [ ] The button goes to `aspiredwebsites.com/billing/trial/.../manage/`,
      **not** to stripe.com.
- [ ] `reminder_sent_at` is now stamped.
- [ ] Running the command again sends **nothing** (it is a daily job and
      must be safe to run twice).

Celery beat runs this daily at 8am in production. **Staging has
celerybeat stopped**, so there it only runs by hand.

### 4.2 Conversion

End the trial immediately so the first real invoice is raised:

```bash
stripe subscriptions update sub_XXXX --trial-end=now
```

- [ ] A $97 invoice is paid in Stripe.
- [ ] GHL: `paid-<slug>` added, **both** `trial-started-<slug>` and
      `trial-ending-<slug>` removed.
- [ ] `TermsAcceptance.converted_at` is now set.
- [ ] **Voice only:** `slot_consumed` is now True, and the `/pricing/`
      scarcity counter has gone down by one.

Then resend that same `invoice.paid` event from the Dashboard:

- [ ] Skipped by the ledger. The counter does **not** move twice.

### 4.3 Cancellation on our own page

From the trial-started page or the reminder email, click the cancel
link. It goes to `/billing/trial/<slug>/manage/?ref=<uuid>`.

- [ ] The page is **ours**: site header, site footer, our branding. No
      redirect to stripe.com anywhere.
- [ ] It opens with **no login**, which is the point: a buyer from ten
      minutes ago has no password yet.
- [ ] It restates the amount and the charge date.
- [ ] Cancelling is **one click**, no retention interstitial.
- [ ] The confirmed page says you will not be charged AND that it keeps
      working until the trial end date.
- [ ] Stripe shows `cancel_at_period_end` (not deleted, so they keep
      the free days they have left).
- [ ] `TermsAcceptance.cancelled_at` is set.
- [ ] Reloading the page shows the confirmed state, and the cancel
      button is gone.
- [ ] `python manage.py send_trial_reminders --dry-run` no longer lists
      them. Somebody who cancelled must never get a "you are about to be
      charged" email.
- [ ] GHL gains `cancelled-<slug>` once the subscription actually ends
      (use `stripe trigger customer.subscription.deleted`, or delete the
      subscription in the Dashboard, to see it now).

### 4.4 The cancel link expires

```bash
# 15 days ago, i.e. past the 14-day window
python manage.py shell -c "
from billing.terms_models import TermsAcceptance
from django.utils import timezone
from datetime import timedelta
row = TermsAcceptance.objects.latest('accepted_at')
row.accepted_at = timezone.now() - timedelta(days=15)
row.save()
print('aged', row.pk)
"
```

- [ ] `/billing/trial/<slug>/manage/?ref=<uuid>` now returns **410**
      and a page saying the link expired, with a sign-in button.
- [ ] POSTing to that same URL also 410s, so the expiry is not just
      cosmetic on the GET.

### 4.5 Payment failure

Add card `4000 0000 0000 0341` (succeeds at setup, fails on charge) as
the default payment method, then end the trial.

- [ ] GHL gains `payment-failed-<slug>`.

### 4.6 The custom portal cancel no longer 500s

Sign in as the client, go to the portal subscriptions page, cancel.

- [ ] You get a **success message naming a date**, not an error page.
      This was broken for every subscriber before this branch.

---

## Part 5. GHL outage behaviour

Break the key and run one signup:

```bash
# in .env
GHL_API_KEY=
```

- [ ] The checkout still completes and the trial still starts.
- [ ] Django admin → Outreach → **Failed lead submissions** has a row
      with `payload.source = "billing.trial_ghl"` and the email.
- [ ] Stripe shows the webhook returned **200** (a 500 would make Stripe
      retry the whole event).

Restore the key afterwards.

---

## Part 6. Test suite

```bash
# Targeted, per CLAUDE.md. ~2 minutes.
python manage.py test billing public outreach voice clients.tests_never_bundled

# The trial work specifically. ~15 seconds.
python manage.py test billing.tests_trial_checkout \
                      billing.tests_trial_webhooks \
                      billing.tests_trial_portal \
                      billing.tests_trial_reminders \
                      public.tests_site_consistency
```

Do not run the full suite casually: it is 2192 tests and about 16
minutes.
