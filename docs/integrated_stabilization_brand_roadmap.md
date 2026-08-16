# Integrated Stabilization and Brand Roadmap

Status: execution plan. This document assigns every item in
`BRAND_REMEDIATION_HANDOFF.md` to a release gate alongside the
`ClientProfile`/`Project` to `Account`/`Website` cutover. It does not turn an
unverified statement into an approved public claim.

## Operating rules

- The data-model cutover remains the critical technical path.
- Urgent public truth corrections do not wait for legacy-table removal.
- A brand change may ship only when the corresponding fact is marked
  `APPROVED` in `docs/brand_fact_matrix.md` or the change removes an
  unsupported claim without replacing it with a new one.
- Pricing, packages, and social deliverables render from active database
  records. Public templates do not become a second source of truth.
- Seed changes and production-row corrections are separate deliverables.
  Updating a seed command does not repair an existing production row.
- Each work package gets its own focused commit after the currently running
  Account/Website Wave 1 work is committed and the worktree boundary is
  clear.
- No public deployment is bundled with a schema cutover unless both releases
  independently pass their gates and a rollback path exists for each.

## Wave map

### Wave 0 — Foundation and fact gate

Technical work:

- Finish the isolated development, test, rehearsal, and production settings.
- Keep the parity auditor and repeatable real-data rehearsal as deployment
  gates.
- Complete and commit the currently running Wave 1 regression run before
  starting overlapping scheduler, public, core, or client-template edits.

Brand work:

- Populate `docs/brand_fact_matrix.md` with evidence and owner decisions.
- Inventory public copy, structured data, seeded database copy, transactional
  copy, contracts, checkout language, and policy pages for each fact.
- Record a canonical vocabulary for the strategy call, delivery range,
  operating location, service areas, cancellation, hosting, and Denis Law
  Group's relationship to Aspired.

Exit gate:

- Every P0 fact is either `APPROVED`, `REJECTED`, or explicitly marked as a
  blocker. Repository text is evidence of current behavior, not proof that a
  business or legal statement is true.

### Wave 1 — Account, identity, authentication, and urgent truth corrections

Technical work:

- Complete Account-canonical creation, editing, authentication, and portal
  resolution as defined in `docs/account_website_cutover.md`.
- Deploy and observe the legacy fallback before removing it.

Brand Release 1A — claims that can be made safer without unresolved policy:

- Correct Denis Law Group everywhere so the site says Aspired maintains and
  improves the existing WordPress site; never say Aspired built, launched,
  hand-coded, or replaced it.
- Remove the `2–3 contacts/week` result until its source, measurement window,
  client approval, baseline, and exact Aspired work are documented. Restore a
  qualified metric only after that evidence exists.
- Fix broken links and obvious copy defects, including the reported
  `Istarted` typo.
- Replace unsupported absolute claims with neutral, truthful wording. This
  includes unverified Fortune 500 experience, guarantees, delivery promises,
  security absolutes, and claims that Aspired verifies a law firm's legal or
  ethical compliance.
- Correct both the seed source and the existing production CaseStudy row. Use
  a reviewed management command or data migration with a dry-run mode rather
  than treating seed data as a production update.

Brand Release 1B — approved facts and policies:

- Reconcile operating location, service areas, legal address, structured
  data, footer, About, Contact, location pages, email templates, and invoice
  identity.
- Reconcile the refund/guarantee, governing law, recurring cancellation, and
  annual-hosting language across pricing, refund, terms, checkout, contracts,
  and proposals.
- Publish one evidence-backed delivery range and one approved scope
  description. Pricing values and tier entitlements remain database-backed.

Dependencies:

- Release 1A can start immediately after the current Wave 1 commit boundary.
- Release 1B is blocked by the corresponding owner/legal decisions in the
  fact matrix.

Exit gate:

- `core`, `public`, and any changed client/billing tests pass.
- A phrase scan finds no obsolete Denis build claims or contradictory policy
  language.
- Production CaseStudy data is reviewed before and after the data correction.
- Manual browser QA passes for home, About, Contact, portfolio index/detail,
  law-firm, pricing, refund, terms, privacy, and error pages.

### Wave 2 — Delivery lifecycle and one conversion path

Technical work:

- Cut contracts, intake, onboarding, delivery stages, revisions, documents,
  and support to Website ownership.
- Move the onboarding gate from the legacy profile to the canonical Account
  and Website state.

Brand Release 2A — conversion contract:

- Make `Book a Free 30-Minute Strategy Call` the canonical sales CTA after
  its name and duration are approved.
- Route booking/schedule CTAs to `/design/schedule/`, general messages to
  Contact, and keep the audit as a secondary lead path.
- Reserve kickoff language for customers who have already bought.
- Remove plan-selection cross-sells from the pre-sale scheduler.
- Make scheduler submission a CSRF-protected POST with explicit failure
  handling and a usable non-JavaScript fallback. Do not put lead details in a
  query string.
- Decide whether phone is optional using the actual follow-up workflow, then
  keep Contact and Scheduler behavior consistent.
- Keep Client Login visually secondary to the primary sales action.

Dependencies:

- Scheduler edits wait until the current Account/Website Wave 1 changes to
  `scheduler/views.py` have landed or been deliberately separated.
- Canonical call wording and phone requirements must be approved in the fact
  matrix.

Exit gate:

- `public`, `scheduler`, and `core` tests cover CTA targets, method/CSRF,
  validation, failure behavior, and the no-JavaScript path.
- Browser QA covers keyboard use, mobile layout, visible focus, field errors,
  success/failure states, analytics events, and privacy-safe URLs.

### Wave 3 — Billing, infrastructure, and policy enforcement

Technical work:

- Cut Stripe checkout/webhooks, payments, subscriptions, domains, droplets,
  and vault ownership to Account/Website.
- Reconcile Whitehead Wellness's canonical `fully_paid` status with real
  payment evidence before relying on it as a launch gate. Do not manufacture
  a PaymentRecord, contract, or timestamp.

Brand Release 2B — transaction consistency:

- Ensure approved refund, guarantee, cancellation, hosting, delivery, and
  ownership language is identical in pricing, checkout, contracts,
  proposals, invoices, and post-purchase email.
- Distinguish month-to-month recurring services from separately billed annual
  hosting.
- Confirm that marketing wording matches the actual Stripe and cancellation
  implementation.

Exit gate:

- `billing`, `contracts`, `clients`, and `vault` targeted tests pass.
- Test purchases cover deposit, final payment, maintenance, hosting, refund
  presentation, and cancellation presentation without creating live charges.
- The parity gate passes before and after the deployment rehearsal.

### Wave 4 — Integrations, scheduled work, and social source of truth

Technical work:

- Cut Moonieful sync, scheduled tasks, social services, and follow-ups to
  canonical ownership.

Brand Release 2C — service consistency:

- Render social tiers and deliverables from active `ServiceTier` and
  `TierFeature` rows through shared presentation code.
- Remove hard-coded platform counts and posting promises from public pages.
- Keep generic pricing industry-neutral; use law-firm examples only on the
  law-firm page.
- Verify that outbound email, follow-up, and social wording uses the approved
  location, cancellation, and service terminology.

Exit gate:

- `social`, `scheduler`, `outreach`, `sync`, and affected billing/public tests
  pass.
- A repository scan finds no public hard-coded social tier entitlements.

### Wave 5 — Reporting, proof, security, and professional-responsibility copy

Technical work:

- Cut reporting, analytics, uptime, scans, GBP, and session recording to
  Website ownership.

Brand Release 3:

- Add structured proof fields only when they are needed to prevent a real
  ambiguity: engagement type, metric source, baseline, measurement window,
  approval status, and evidence date. Do not build speculative proof fields.
- Show metrics only when their evidence and publication permission are
  complete; otherwise present portfolio work without performance numbers.
- Rewrite security copy around controls that can be demonstrated in code,
  configuration, operations evidence, or an approved credential-verification
  link. Do not invent audit cadence, recovery objectives, insurance, or
  incident-response claims.
- State that Aspired supports client-provided legal and ethical requirements;
  the attorney remains responsible for compliance. Describe intake data as
  sensitive prospective-client information unless counsel approves stronger
  terminology.
- Complete the focused tone pass: preserve directness, ownership,
  transparent pricing, and security differentiation while removing repeated
  fear copy, competitor attacks, and unsupported absolutes.
- Review logo legibility, founder portrait/credential treatment, header CTA
  hierarchy, alt text, and accessible page structure.

Exit gate:

- `reporting`, `public`, `core`, and affected client tests pass.
- Every displayed metric links internally to a source record and approval.
- Every material security claim has an evidence note.
- Manual accessibility and responsive QA covers the public templates in the
  handoff.

### Wave 6 — Legacy removal and final consistency audit

Technical work:

- Remove remaining legacy reads and writes only after Waves 1–5 have been
  deployed and observed.
- Rehearse the destructive migration, take a verified backup, pass the strict
  parity gate twice, and then remove legacy tables in a separately approved
  deployment.

Brand work:

- Run a final repository and production-data scan for obsolete claims,
  duplicate pricing/scope sources, stale location text, and retired CTA
  wording.
- Verify canonical URLs, redirects, metadata, schema, sitemap, and policy
  links after the last template/model changes.

Exit gate:

- No runtime code reads `ClientProfile` or `Project` as a canonical source.
- No public claim depends on an unapproved fact-matrix row.
- Full pre-production test and browser regression passes before production
  deployment.

### Wave 7 — Internal workflow and resilience

- Redesign the overloaded admin navigation around owner workflows after the
  canonical model and public contracts are stable.
- Split oversized modules along tested service boundaries.
- Harden optional integrations so unavailable Redis, third-party APIs, and
  browser tooling degrade without taking unrelated request paths down.
- Add operational dashboards for parity, billing evidence, sync health,
  deliverability, and failed scheduled work.

## Priority coverage

| Handoff priority | Assigned work |
|---|---|
| P0 verified facts | Wave 0 fact gate |
| P1 Denis Law Group | Wave 1 / Brand Release 1A |
| P2 policy/legal conflicts | Wave 1B and Wave 3 transaction verification |
| P3 one conversion path | Wave 2 / Brand Release 2A |
| P4 timeline and scope | Wave 1B, enforced again in Wave 3 |
| P5 social duplication | Wave 4 / Brand Release 2C |
| P6 location and founder | Wave 1B; presentation polish in Wave 5 |
| P7 verifiable proof | Wave 5 / Brand Release 3 |
| P8 security claims | Wave 1A removals; Wave 5 evidence-backed rewrite |
| P9 law-firm legal/ethical wording | Wave 5 / Brand Release 3 |
| P10 friction and accessibility | Wave 2; visual follow-up in Wave 5 |
| P11 tone and information hierarchy | Wave 1A high-risk removals; Wave 5 focused pass |

## Work-package record

Every brand work package closes with a short record containing:

1. Current behavior reproduced.
2. Truth sources inspected, including production data where relevant.
3. Proposed fix and credible alternative.
4. Chosen implementation and why.
5. Files, migrations, commands, and production rows changed.
6. Targeted tests and manual browser checks completed.
7. Remaining decisions and exact deployment/rollback commands.

This record is part of the implementation, not optional release prose.
