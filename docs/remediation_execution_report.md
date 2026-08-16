# Remediation and Stabilization Execution Report

Running record for the integrated stabilization + brand program defined in
`docs/integrated_stabilization_brand_roadmap.md`. Updated as work proceeds.

## Operating envelope

`docs/brand_fact_matrix.md` currently carries **one** `APPROVED` row — the
Denis Law Group relationship. Every other public-fact row is `PENDING`.

The roadmap's operating rule is therefore the binding constraint on all
brand work:

> A brand change may ship only when the corresponding fact is marked
> `APPROVED` in `docs/brand_fact_matrix.md` or the change removes an
> unsupported claim without replacing it with a new one.

So this program may:

- correct Denis Law Group (approved fact),
- **remove or neutralize** unsupported claims,
- centralize wording so one future decision updates one place,
- make database records the single source of truth for entitlements.

It may **not** author a jurisdiction, refund policy, guarantee, delivery
range, operating location, service-area list, call name/duration, founder
credential wording, or any client metric. Those stay blocked and are listed
at the end for owner/legal decision.

## Issue register

| # | Issue | Wave | Decision / status |
|---|---|---|---|
| 1 | Full-suite run before this program lost its summary line to a shell pipe (`manage.py test 2>&1 \| tail -25`), so the pre-existing pass count is unverified | 0 | Deferred: complete suite runs once at the end per instruction; count reported there |
| 2 | `2–3 contacts/week` and the `Istarted` typo appear nowhere in the source tree | 0 | Not a source defect. Either never published or lives only in database-edited content. Recorded; no invented fix |
| 3 | `seed_case_studies.py` docstring cites owner confirmation (2026-08-02) that Denis was a NEW practice launch; the handoff carries later owner direction that Aspired did not build it | 1 | Later direction wins; the approved fact-matrix row governs. Superseded note recorded in the seed |
| 4 | 30-day money-back guarantee exists in `clients/contract_template.py` (§7, §10), not only in marketing | 1B/3 | Materially changes P2: the guarantee may be contractually real. Not removed unilaterally — blocked on owner/legal decision |

## Wave status

| Wave | Status |
|---|---|
| 0 — foundation, isolation, fact gate | **Complete** |
| 1 — Account identity (technical) | **Complete** |
| 1 — Brand Release 1A | **Complete** |
| 1 — Brand Release 1B | **Blocked** — every fact PENDING |
| 2 — Brand 2A conversion/scheduler safety | **Complete** (technical lifecycle cutover not started) |
| 3 — Whitehead reconciliation | **Complete**; rest of billing cutover not started |
| 4 — Brand 2C social source of truth | **Complete**; sync/task cutover not started |
| 5 — Brand 3 security/legal wording (removals) | **Partial**; reporting cutover not started |
| 6 — legacy-runtime removal prep | Not started |
| 7 — internal workflow and resilience | Not started |

Waves 2–6 each contain a *technical* Account/Website cutover (contracts,
intake, delivery stages, Stripe/webhooks, domains, droplets, vault,
Moonieful sync, reporting) that is not started. Those are large, and the
roadmap gates each behind deploying and observing the previous wave — which
cannot happen locally. The brand releases attached to those waves that did
not depend on a deploy were completed early where they were independent.

## Deployment record — staging, 2026-08-16

Deployed to **staging only** (`aspired-staging`, 167.99.154.2). Production
was not touched: the deploy request did not name prod, and CLAUDE.md makes
staging the default target.

Commits on `main` (`c7c4bc4..499658e`), pushed:

| Commit | Scope |
|---|---|
| `c86de1d` | settings split (development/test/production/rehearsal) |
| `7caf0fa` | Account/Website parity gates, safe backfills, Wave 1 cutover |
| `3b94151` | Denis Law Group correction + unsupported claim removals |
| `c9861af` | scheduler POST/no-JS safety, pre-call cross-sells removed |
| `499658e` | governing law, guarantee, location, DB-backed social tiers |

Steps executed on staging: `git pull`, `pip install -r requirements.txt`,
`migrate` (0052, 0053, 0054 applied), `collectstatic` (0 changed, 195
unmodified), supervisor restart, then `aspiredwebsites-celerybeat` stopped
again — the restart starts it and staging deliberately runs without it.

### Results on staging

- `remediate_case_studies --apply` corrected the Denis row (engagement_type,
  platform, summary, challenge, solution, results). Re-run reports
  "already correct" — idempotent.
- Parity audit before backfills: **37 errors, 1 warning**. Staging carries
  its own drifted data, which made it a live test of the deployed tooling.
- After `refactor_to_accounts` + both backfills: **0 errors**. Second pass
  wrote nothing (0 accounts, 0 websites, 0 dependent FKs).
- One `website-field-conflict` remains and is correct behaviour: package
  and payment_status genuinely disagree between the legacy and canonical
  rows on a staging test record. It is reported for a decision, not
  auto-resolved.
- Smoke test: 11 key pages return 200. Denis page shows "What We Improved"
  and "maintained and improved", with no build claim. About shows "Based in
  Georgia". Terms shows "State of Georgia". Booking form carries
  `method="post"`.

### Not run — needs production

`verify_website_payment whitehead-wellness` could not run: that Website
exists only in production. Staging holds a single unrelated site. The
command is deployed and ready; it needs an explicit production
authorisation to execute.

## Delivered

### Brand Release 1A — Denis Law Group (P1)

Approved fact: Aspired did not build the site; it is an existing WordPress
site Aspired maintains and improves.

- `CaseStudy.engagement_type` + `platform` (migration `0053`). Blank means
  "not verified" and renders neutral wording — a row nobody has reviewed
  never inherits a build claim.
- `work_heading` / `relationship_label` / `image_alt` properties so a
  heading, a badge and generated alt text cannot drift apart.
- `seed_case_studies` Denis entry rewritten to maintenance/improvement.
  Specific improvements are deliberately **not** itemised — which changes
  Aspired made and may describe publicly is still unproven.
- `remediate_case_studies` (new, dry-run default, idempotent) corrects
  existing production rows. `seed_case_studies` cannot: without `--force`
  it never touches an existing row, and with `--force` it overwrites every
  field of every study.
- Templates corrected: `case_study_detail.html`, `home.html`,
  `_case_study_visual.html`, `service_law_firm_web_design.html`.

**Model-vs-template comparison.** The handoff asked for both options to be
weighed. Neutral templates alone were rejected: alt text is *generated*
from the study, so one shared string has to describe four different
relationships, and the portfolio genuinely holds both built and maintained
work. The field is the smallest change that makes the misstatement
structurally impossible rather than repeatedly corrected by hand.

### Brand Release 2C — social source of truth (P5)

- `core/_social_tier_cards.html` renders name, price and entitlements from
  active `ServiceTier` / `TierFeature` rows; pricing and the service page
  share it.
- Removed hardcoded `$399/$699/$999` **and** the JSON-LD
  `priceRange: "$399 - $999 / month"` — structured data is still a public
  price claim. Both now derive from the database, so a pricing-admin edit
  updates the page and its schema together.
- Removed contradicted entitlements: the page advertised one channel on
  Basic and two on Standard while the database said two and three, plus ad
  management, lead capture and weekly check-ins that no `TierFeature`
  contained.

### Brand 2A — conversion safety (P3, P10)

- The booking form declared **no HTTP method**. A submit reaching the
  browser (script failure, Enter in a text field before the handler binds)
  became a GET, putting name, email, phone and project description into the
  URL — and from there into logs, `Referer` and analytics. Now
  `method="post"`, with the view answering that POST explicitly.
- Added a `<noscript>` state and a POST fallback notice pointing at
  Contact. The fallback stores nothing and sends nothing: a booking with no
  chosen slot is not a booking, and inventing lead capture there would be a
  product decision, not a fix.
- Rate limit added to the page POST path (30/h per IP).
- Removed pre-call plan cross-sells (`show_addons` off on all three
  pre-sale schedulers).
- "30-minute kickoff call" → "strategy call"; kickoff is post-sale.

### P8 / P9 — unsupported absolutes and legal wording

All are removals or neutralizations, so no PENDING fact was needed:

- "The same standards a Fortune 500 would demand" — removed.
- "That's donations to Meta", "We weaponize it", "money set on fire"
  (twice) — replaced with factual statements.
- "privileged intake" → "sensitive prospective-client information".
- "a bar complaint waiting to happen" → factual confidentiality,
  reputational and regulatory risk.
- The law-firm hub claimed Aspired verifies bar advertising compliance
  before launch, contradicting the detailed service page. Now states
  Aspired builds so required disclaimers are easy to place and maintain,
  and confirming requirements stays with the firm.

### Regression tests added

- `public/tests_brand_consistency.py` — social plans render from the
  database, a tier price change flows through to the page, no hardcoded
  prices or entitlements, no Fortune 500 / fear copy / privileged intake /
  bar-verification claim, and a repository-wide multiline `{# #}` scan.
- `scheduler/tests_conversion.py` — form method, CSRF token, `<noscript>`,
  POST fallback stores nothing, no pre-call cross-sell, no kickoff wording.
- `core/tests.py` — Denis is never described as an Aspired build on any
  surface, engagement type drives wording, unverified type stays neutral,
  and `remediate_case_studies` reports before it writes and is idempotent.

## Wave 0 — foundation, isolation, fact gate

### Already delivered before this program

- `settings_development` / `settings_test` / `settings_production` /
  `settings_rehearsal` split, with the rehearsal module refusing to start
  against a non-rehearsal database and blanking every outbound credential.
- `clients/parity.py` + `audit_account_website_parity` with error /
  warning / operational severities.
- `repair_account_website_parity` (manifest-driven; refuses to guess).
- Synthetic rehearsal (`seed_rehearsal_dataset`) and a real-data rehearsal,
  both reaching a zero-finding strict gate with a zero-write second pass.
- Regression suites `clients/tests_parity.py` and
  `clients/tests_migration_regression.py`.

### Content inventory (this program)

Scans run across templates, views, seeds, tests and management commands for
every phrase family named in the handoff's completion checklist. Findings
drive the wave work below; the raw results are recorded per priority.
