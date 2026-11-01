# Environment variable reference

`.env` is gitignored, and so is every `.env.*` spelling including
`.env.example` (see `.gitignore` line 22). So there is no committable
example file to copy. This document is the substitute: it lists the
variables added since the Oct 2026 repositioning, with their defaults,
so a new environment can be brought up without reading `settings.py`.

CLAUDE.md's "Environment Variables" section remains the full list for
the older variables. CLAUDE.md is itself gitignored, which is the other
reason this file exists.

Every variable below has a working default. None of them need to be set
for the site to boot.

## Free trial and Terms of Service (Nov 2026)

| Variable | Default | What it does |
|---|---|---|
| `TRIAL_DAYS` | `14` | Length of the free trial on the two $97/mo add-ons. Passed to Stripe as `subscription_data.trial_period_days` and used to compute the first-charge date shown at checkout. Changing it affects new checkouts only; a running trial keeps the `trial_end` Stripe gave it. |
| `TERMS_VERSION` | `2026-11-01` | Stamped onto every `billing.TermsAcceptance` row and shown at the top of `/terms/`. Bump it together with the terms copy. |

Deliberately **not** added, despite being in the original brief:

- `STRIPE_PRICE_VOICE_RECEPTIONIST`
- `STRIPE_PRICE_REVIEW_AUTOMATION`

`ServiceTier.stripe_price_id` is the source of truth for prices
(CLAUDE.md, "Pricing Management"), and the receptionist price is a
cohort ladder: `voice/cohorts.py` mints a new Stripe Price per rung and
repoints `ServiceTier` at it. An env var would pin new signups to the
opening price forever and sell a $147 product for $97.

## The trial cancel link and nginx access logs

`/billing/portal/stripe/?ref=<uuid>` is the no-password cancel link on
the trial-started page. The `ref` is a credential for the length of the
trial, and the application never logs it: Django's own 4xx logging
records `request.path`, which excludes the query string, and no logger
call in `billing/portal_views.py` carries it.

**nginx does log it.** The default `combined` log format records the
full request line including the query string, so these refs land in
`/var/log/nginx/access.log` on staging and prod. They expire with the
trial, so the exposure is bounded, but if the access log is shipped
anywhere off-box that is worth knowing. To strip it, log `$uri` instead
of `$request` for that location.

## Per-environment values

`TRIAL_DAYS` and `TERMS_VERSION` should be identical in all three
environments. They are exposed as env vars so a trial length can be
changed without a deploy, not so environments can disagree.

| Environment | Where `.env` lives |
|---|---|
| Local (Windows) | `AspiredWebsitesRevamped\.env` |
| Staging `167.99.154.2` | `/var/www/aspired/app/.env` |
| Prod `161.35.108.209` | `/var/www/aspired/app/.env` |

`.env` never arrives via `git pull`. Edit it on the server. A change
read by provisioning or a Celery task needs the **celery** worker
restarted, not just gunicorn. Neither variable here is read by Celery,
so `supervisorctl restart aspiredwebsites` is enough for both.
