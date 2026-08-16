# Brand Fact Matrix

Status: owner and evidence gate. A public claim is not approved merely because
similar wording already exists in the repository.

Allowed statuses:

- `APPROVED` — Zachery has approved the exact public meaning and its evidence.
- `REJECTED` — do not publish this claim.
- `PENDING` — research or an owner/legal decision is still required.
- `REMOVE` — remove the unsupported claim without substituting a new fact.

| Fact or policy | Status | Current evidence | Required decision or evidence |
|---|---|---|---|
| Public location statement | APPROVED | Owner 2026-08-16 approved the exact wording: **"Based in Georgia. Serving clients nationwide."** | IMPLEMENTED as `core.site_facts.LOCATION_STATEMENT`, used by About and page metadata. The footer/schema keep the more specific "Warner Robins, GA" NAP, which is consistent (a city within the stated state) and must stay matched to external profiles (GBP, Bing, Apple, LinkedIn) |
| Atlanta, San Antonio, and nationwide are service areas, not all physical offices | APPROVED | Owner 2026-08-16: US-wide is accurate; the approved statement names no city but Georgia | IMPLEMENTED: removed the "Based in San Antonio and Atlanta" claims from About and two meta descriptions. Location pages already present both as service markets served from Georgia, not offices |
| Legal registration name and mailing address | PENDING | Must be reconciled across footer, policies, invoices, contracts, and business records | Provide the exact legal entity name and public/legal mailing address |
| Governing law and venue | APPROVED | Owner decision 2026-08-16: **Georgia**. Corroborated by both contract templates, which already said State of Georgia (§8, §12) | IMPLEMENTED: Terms switched from Texas/San Antonio to Georgia; refund page venue generalised to the State of Georgia. Outstanding: the specific **county** for venue (refund page had said Fulton; the operating base is elsewhere in Georgia) |
| Canonical sales call is a free 30-minute strategy call | PENDING | Handoff recommendation | Confirm name, duration, price, and destination URL |
| Typical delivery range | PENDING | Repository reportedly contains 3 weeks, 4 weeks, and 6 weeks | Approve one evidence-backed range and the conditions that can extend it |
| Refund or guarantee policy | APPROVED | Owner decision 2026-08-16: **keep and advertise** the 30-day money-back guarantee. The contract already grants it: full refund of the build fee within 30 days of signing (§7/§10) | IMPLEMENTED: pricing badge retained; Terms and refund policy restated to match the contract, with the guarantee taking precedence over milestone treatment inside the 30-day window |
| Recurring-service cancellation | PENDING | Business rules say month-to-month and also require 30 days' notice | Confirm effective cancellation timing and charges during the notice period |
| Hosting billing and cancellation | PENDING | Hosting is listed annually while other services are month-to-month | Confirm renewal, cancellation, refund, migration, and data-retention handling |
| Social tier deliverables | PENDING | Database is authoritative; public service-page counts reportedly disagree | Review active `ServiceTier`/`TierFeature` rows and approve them as the public contract |
| Aspired's Denis Law Group relationship | APPROVED | Owner direction in the handoff: Aspired did not build the WordPress site and maintains/improves it | Public wording must remain maintenance/improvement only. IMPLEMENTED 2026-08-16 via `CaseStudy.engagement_type='maintained'`, corrected seed copy, and `remediate_case_studies` for existing rows |
| Denis Law Group receives about 2–3 contacts per week | REMOVE | Owner 2026-08-16: never been published; the relationship is simply that Aspired is her website guy | Do not publish. The figure appears nowhere in the source tree and none was added |
| Publishable Denis Law Group improvements | PENDING | Not itemized in the handoff | List changes Aspired actually made and may publicly describe |
| Founder credentials and verification link | PENDING | AGENTS.md reports a master's degree and CISSP; public verification and exact degree wording are not documented here | Confirm exact credential names, awarding institution where appropriate, and approved verification link |
| Publishable security controls | PENDING | Controls exist across code/configuration, but marketing claims require a separate evidence inventory | Approve only controls supported by current implementation and operations evidence |
| Contact-form phone field is required | PENDING | Current implementation must be compared with actual follow-up workflow | Decide whether email-only leads can be served; default recommendation is optional phone |
| Founder portrait may be published | PENDING | Handoff says to use one only if authorized | Approve the exact asset and placement or reject portrait use |
| Client result metrics may be published | PENDING | No blanket permission documented | Require evidence window and client approval per metric, not one global approval |

## Implementation notes (2026-08-16)

- The `2–3 contacts per week` figure appears **nowhere in the source tree**
  and was never published. Owner confirmed it should stay unpublished.
- The reported `Istarted` typo does not exist in the current source. If it
  is visible in production it is in database-edited content, not a template.
- The 30-day money-back guarantee was never marketing-only: it is written
  into `clients/contract_template.py` §7 and §10 as a full refund of the
  build fee within 30 days of signing. The owner elected to keep and
  advertise it, so public copy was aligned to the contract rather than the
  badge being removed.
- The refund policy also states a lifetime/90-day fix-it commitment
  (`core/templates/core/refund_policy.html`). That sits *after* the 30-day
  guarantee window and does not contradict it, but the two should be read
  together when the policy is next reviewed by counsel.
- Payments that arrive outside Stripe are recorded as a named operator
  attestation on `Website.payment_verified_*` via
  `manage.py verify_website_payment`. No PaymentRecord, invoice, contract
  or timestamp is ever fabricated to make a build look paid.

## Evidence notes

- Business records, signed contracts, Stripe configuration, approved client
  correspondence, analytics exports, and credential-verification pages can be
  evidence.
- Existing marketing copy, seed commands, and old database rows describe
  current behavior but do not prove the claim is true.
- Legal and policy rows require owner/counsel approval. Engineering can find
  contradictions and make implementation consistent; it cannot choose the
  governing bargain.
- When a fact remains pending, the safe implementation is to remove the
  unsupported specificity or leave the affected release blocked.
