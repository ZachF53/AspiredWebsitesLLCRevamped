# Aspired Websites Brand Remediation — Claude Code Handoff

## Mission

Clean up the public Aspired Websites brand and conversion experience after a seven-person fresh-buyer review. Preserve what already works: the security-first positioning, transparent pricing, client ownership, direct founder access, and candid tone.

Do **not** blindly implement the suggested fixes below. For every work item:

1. Reproduce and document the current behavior in the local code and, where relevant, the live site.
2. Identify the actual source of truth, including database-backed content, seed commands, tests, legal pages, views, and templates.
3. Develop your own proposed fix (“Claude option”).
4. Compare it against the suggested fix in this document.
5. Choose the better option based on truthfulness, buyer clarity, conversion friction, accessibility, security, SEO, maintainability, and regression risk.
6. Record the choice and reasoning in the implementation summary.
7. If a required business or legal fact cannot be proven from the repository, stop on that item and ask Zachery. Do not invent a value.

## Non-negotiable constraints

- Do not fabricate results, testimonials, security controls, timelines, credentials, locations, or client work.
- Do not describe a maintained or improved website as built by Aspired.
- Pricing and tier features are database-backed through `billing/pricing_models.py`; do not create a second hardcoded pricing source.
- Preserve the existing security controls on public forms: CSRF, rate limits, server-side validation, honeypots, upload validation, and privacy safeguards.
- Preserve canonical URLs, structured data, sitemap behavior, and legitimate location landing pages unless research supports a deliberate change.
- Follow `AGENTS.md`, especially the Django template-comment rule. Every `{# ... #}` comment must open and close on the same physical line. Use `{% comment %}` for multiline comments.
- Run targeted tests only for modified apps. Do not run the full suite unless the final change genuinely crosses enough apps to require it.
- Inspect the current working tree before editing and preserve unrelated user changes.

---

## Priority 0 — Establish verified brand facts

Before changing public copy, create a short fact matrix covering:

- Primary operating location
- Legal registration/address and governing jurisdiction
- Service areas
- Canonical call name and duration
- Typical website delivery range
- Exact build refund/guarantee policy
- Recurring-plan cancellation notice
- Hosting billing/cancellation distinction
- Current social-plan deliverables
- Denis Law Group relationship, work performed, and result attribution
- Security controls that can be publicly supported
- Founder credential-verification information that may be published

### Suggested fix

Treat Warner Robins, Georgia as the operating base because current footer, schema, location-page comments, and tests identify it as the master record. Describe Atlanta, San Antonio, and nationwide work as service areas rather than staffed offices. Keep legal registration/address separate from the public operating location.

However, governing law is unresolved in the repository: the refund policy says Georgia/Fulton County while the Terms say Texas/San Antonio. Research incorporation records, current contracts, and business intent. Ask Zachery if the answer is not provable. This is a legal/business choice, not a copy-editing guess.

### Architecture comparison required

Compare:

- A small centralized public-facts module/context processor plus regression tests.
- Keeping copy in templates but adding strong cross-page consistency tests.

Choose the least complex approach that prevents future drift. Do not move database-authoritative pricing into static constants.

### Acceptance criteria

- One documented value exists for every verified fact.
- Any unresolved fact is explicitly listed for owner decision.
- No public-copy implementation begins using an assumed legal fact.

---

## Priority 1 — Correct the Denis Law Group case study immediately

### Verified owner direction

- Aspired Websites **did not build** the Denis Law Group website.
- It is a WordPress website.
- Aspired maintains it and made improvements to the existing site.
- The site now receives approximately 2–3 contacts per week.

### Required research

Before publishing the metric, verify:

- Whether “2–3 contacts per week” is client-reported, CRM-derived, form-derived, or analytics-derived.
- The measurement period and whether it is still current.
- Whether the client approved public use of the number.
- Whether a reliable pre-engagement baseline exists.
- Which improvements Aspired actually performed and can truthfully name.

Do not imply that Aspired caused an increase unless the baseline and attribution support that conclusion.

### Suggested positioning

Use an engagement label such as **“Website Maintenance & Conversion Improvements”** or **“Existing WordPress Site — Maintained and Improved by Aspired.”** Suggested factual copy direction:

> Denis Law Group’s WordPress website was not designed or built by Aspired Websites. We took over ongoing maintenance and made targeted improvements to the existing site. The firm reports receiving approximately 2–3 website contacts per week.

Only replace “targeted improvements” with a specific list after verifying the work performed. If the metric is based on internal records, replace “the firm reports” with the precise source and measurement period.

### Code and data locations to inspect

- `clients/management/commands/seed_case_studies.py`
- `clients/models.py` — `CaseStudy`
- `public/templates/public/case_study_detail.html`
- `public/templates/public/portfolio.html`
- `public/templates/public/service_law_firm_web_design.html`
- `public/templates/public/home.html`
- `public/views.py`
- `core/tests.py`, especially Denis/case-study assertions
- `clients/management/commands/capture_case_study_screenshots.py`
- The existing Denis database row in development and production

### Suggested implementation

1. Remove all Denis claims containing “built,” “built from scratch,” “hand-coded,” “no template,” “no page builder,” “new practice launch,” or equivalent statements unless separately true and documented.
2. Change the challenge/solution/results narrative from a build story to a maintenance and improvement story.
3. Publish the 2–3 contacts/week metric only with accurate attribution and a reasonable measurement window.
4. Replace generic case-study headings and alt text that automatically say “What We Built” or “built by Aspired.” Use neutral language such as “What We Did” and relationship-aware image text.
5. Rework the law-firm service-page proof block so it accurately says Aspired maintains and improves the site.
6. Fix or coordinate fixes for visible issues on the linked Denis site, including the reported malformed Instagram URL and overlapping desktop consultation panel. Do not silently modify the external site unless credentials and authorization are in scope.
7. Update seed data and existing database data. Confirm the deploy process will not restore old claims on the next `seed_case_studies --force` run.
8. Update tests that currently encode the incorrect “new build” story.
9. Recapture the portfolio screenshot only after visible live-site issues are resolved or choose an accurate stable image.

### Model comparison required

Compare:

- Adding a `CaseStudy.engagement_type`/`relationship_type` field so templates distinguish “Built,” “Maintained,” “Redesigned,” and “Consulted.”
- Keeping the model unchanged and making all headings/copy universally neutral.

Prefer the model field if multiple current or future portfolio items require different relationship types and it materially prevents misrepresentation. Prefer neutral templates if the migration would add complexity without meaningful reuse.

### Acceptance criteria

- No public page says or implies that Aspired built the Denis website.
- WordPress is described neutrally and accurately, not hidden or disparaged.
- The 2–3 contacts/week statement is attributed and supportable.
- The case-study seed command, database row, service page, portfolio page, metadata, image alt text, and tests agree.
- No invented before/after improvement is introduced.

---

## Priority 2 — Reconcile guarantee, refund, jurisdiction, and cancellation language

### Current conflicts

- Pricing advertises “30-Day Money-Back Guarantee on All Builds.”
- The refund policy makes the deposit refundable for seven days or until kickoff, uses pro-rata treatment during development, and does not refund completed builds.
- Terms say fees are non-refundable once work begins except for a specific guarantee.
- Refund policy and Terms select different states/courts.
- “Cancel anytime” appears beside separate 30-day-notice language.
- “No annual contracts” can be read as conflicting with annual hosting billing.

### Suggested fix

Unless a real 30-day build refund guarantee is contractually intended, remove that pricing badge. Replace it with a true risk-reducer such as:

- “50% to start · 50% on delivery”
- “Written scope and milestone billing”
- A precisely defined 90-day fix-it commitment, but only if contracts and operations support it

Use “Month-to-month; cancel with 30 days’ notice” for recurring services. Explain separately that hosting is billed annually but is not a long-term services contract.

### Required comparison

Compare removing the guarantee against formally defining and supporting a real build guarantee. Consider contract language, Stripe/refund operations, financial exposure, customer clarity, and enforcement. Do not choose a marketing promise that the contract or business process cannot honor.

### Likely locations

- `public/templates/public/pricing.html`
- `core/templates/core/refund_policy.html`
- `core/templates/core/terms.html`
- `billing/templates/billing/checkout.html`
- Recurring-service templates and database tier copy
- Contract/proposal templates and generated PDFs
- Tests in `core/tests.py`, `billing/tests.py`, and modified apps

### Acceptance criteria

- Pricing, checkout, refund policy, Terms, proposals, and contracts describe the same policy.
- One verified jurisdiction is used where legally appropriate.
- “Cancel anytime” is not used in a way that hides a 30-day notice requirement.
- Annual hosting billing is plainly distinguished from an annual service lock-in.

---

## Priority 3 — Make one conversion path

### Current problem

Visitors understand that they should book a call, but “Book,” “Schedule,” “Strategy Call,” “Consultation,” “Kickoff Call,” “Start Your Project,” the contact form, and multiple scheduler URLs create unnecessary variation. Many booking CTAs lead to `/contact/` rather than the calendar. The scheduler presents maintenance and social-plan cross-sells before the first conversation.

### Suggested fix

- Canonical public action: **“Book a Free 30-Minute Strategy Call.”**
- Route every CTA containing “Book” or “Schedule” to the canonical scheduler, currently `/design/schedule/`.
- Reserve `/contact/` for “Contact,” “Send a Message,” phone, and email.
- Use the audit as the consistent secondary CTA: **“Run the Free Website Audit.”**
- Rename pre-sale “kickoff” language to “strategy call” or “discovery call.” Reserve “kickoff” for paying clients.
- Remove all plan checkboxes and 10% cross-sells from the initial scheduling experience. Surface relevant plans after discovery through the proposal/follow-up flow.
- Keep phone optional in scheduling. Research whether it should also be optional on the general contact form.

### Code locations

- `scheduler/views.py` — `SERVICE_CONFIG` and `show_addons`
- `scheduler/templates/scheduler/schedule.html`
- `core/static/js/schedule_call.js`
- `scheduler/emails.py`
- `core/templates/emails/schedule_confirmation.html`
- `core/templates/emails/schedule_admin_notification.html`
- Booking CTAs throughout `public/templates/public/`
- `core/static/js/events.js`
- Scheduler canonical, sitemap, and route tests

### Required comparison

Compare one universal scheduler with optional service preselection against keeping service-specific scheduler variants. Preserve useful attribution without presenting different promises or creating duplicate-search pages.

### Security check

The scheduler form currently relies on JavaScript `fetch()` and lacks an explicit HTML method. Research graceful failure. At minimum, prevent personal details from falling into a query string if JavaScript fails; preferably provide a safe POST fallback or a clear no-JavaScript state. Preserve CSRF enforcement.

### Acceptance criteria

- Every booking CTA has the same label family, duration, expectation, and destination.
- “Kickoff” appears only in post-sale contexts.
- No recurring-plan upsell appears before a prospect books the initial call.
- Booking analytics continue to fire without collecting sensitive fields.
- Keyboard, screen-reader, no-JavaScript, validation-error, and mobile flows are tested.

---

## Priority 4 — Establish one truthful timeline and one pricing scope

### Current conflicts

- `billing/management/commands/seed_pricing.py` sets build timelines to three and four weeks.
- Pricing FAQ and most service pages describe roughly six weeks.
- The web-design page contains both a six-week process and an approximately four-week Essential launch.
- Essential page-count language varies.
- Generic pricing includes law-firm-specific practice-area and schema language.

### Suggested fix

Use **“Typically 4–6 weeks after kickoff, depending on scope and content readiness”** across general buyer-facing copy unless actual delivery data supports a different range. Describe package-specific differences as scope differences rather than hard promises.

Make the general pricing copy industry-neutral while clearly explaining law-firm equivalents, for example: “Up to 8 pages; law-firm projects may allocate up to 5 of those to practice-area pages.” Do not duplicate packages unless there is a real operational reason for different products.

### Required comparison

Compare:

- Removing exact `timeline_weeks` badges and using a shared range.
- Extending the pricing model to support a minimum/maximum or display label.
- Retaining package-specific timelines only if real project data proves they are reliable.

Also compare one industry-neutral pricing page against separate law-firm/general package presentations that still draw from the same `ServiceTier` records.

### Acceptance criteria

- Pricing cards, FAQs, service pages, About, scheduler, proposals, and contracts agree.
- Database seed data cannot reintroduce obsolete timelines or features.
- Page counts and included deliverables are unambiguous.
- No template hardcodes an authoritative price.

---

## Priority 5 — Eliminate social-plan duplication

### Current conflict

Database-backed pricing defines Basic as two platforms and Standard as three, while `public/templates/public/service_digital_marketing.html` hardcodes one and two channels plus different ad-management, lead-capture, and check-in promises.

### Suggested fix

Pass active `ServiceTier` social-media plans into the digital-marketing view and render the same names, prices, and `TierFeature` records used by pricing. If the service page needs shorter summaries, derive them from an explicit database field rather than maintaining a second hardcoded entitlement list.

### Required comparison

Compare database-rendered cards against a shared template partial used by pricing and the service page. Choose the option that best prevents drift without forcing the two pages to have identical visual structure.

### Acceptance criteria

- Pricing, service page, scheduler/proposals, checkout, and contracts show identical entitlements.
- `seed_pricing` and pricing-admin edits produce predictable public output.
- No stale hardcoded plan descriptions remain.

---

## Priority 6 — Standardize location and founder identity

### Suggested public wording

> Based in Warner Robins, Georgia. Serving Middle Georgia, Atlanta, San Antonio, and clients nationwide.

Use this only after confirming the fact matrix. Do not present Atlanta or San Antonio as staffed offices if they are service markets.

### Locations to reconcile

- `public/templates/public/about.html`
- `public/templates/public/contact.html`
- `public/views.py` metadata
- `core/templates/base.html`
- `core/templates/core/_schema_site.html`
- Privacy, Terms, and refund pages
- Email signatures, invoices, and CAN-SPAM address usage
- Location pages and their structured data
- External business listings, if deployment scope includes them

### Important distinction

The legal registered address, operating location, service areas, and chosen contractual jurisdiction are different concepts. Do not force all four to use the same city merely for visual consistency.

### Founder presentation

Research whether a current professional portrait and publishable credential identifier/link exist. If they do, compare adding them against the current initials card and generic ISC2 link. Do not publish a certification number or portrait without authorization.

### Acceptance criteria

- A visitor can clearly distinguish where Aspired is based from where it serves clients.
- Structured data matches the verified operating facts.
- Legal/contact addresses remain compliant with their separate purposes.
- “I” versus “we” follows a deliberate rule: founder story may use “I”; company/service commitments should use “Aspired” or “we.”

---

## Priority 7 — Raise proof to match the brand promise

### Suggested fix

- Add verifiable metrics to case studies only where a reliable source exists.
- For every metric, preserve source, measurement window, and permission internally even if the public display is concise.
- Add independent review links and fuller testimonials only with permission.
- Replace the portfolio headline “Real Results” if the page still lacks measured outcomes; “Real Work for Real Businesses” is safer.
- Make case studies explicitly distinguish builds, redesigns, maintenance, and optimization work.
- Show before/after performance, leads, conversion rate, rankings, or security findings only when the comparison method is defensible.

### Data-model comparison required

Research whether `CaseStudy` needs fields for engagement type, metric source/date, baseline, measurement period, and client approval. Compare that against keeping substantiation in internal notes or documentation. Choose a model change only if it will be consistently maintained.

### Acceptance criteria

- Every public result can be supported.
- Portfolio headlines do not promise more than the case studies demonstrate.
- No client relationship is mislabeled.

---

## Priority 8 — Make security claims specific and verifiable

### Current issue

The security positioning is the brand’s strongest differentiator, but claims such as “Fortune 500 standards” and “threats most agencies don’t even know exist” are broader than the public evidence. A dedicated server is isolation, not proof of secure operations.

### Suggested fix

Replace prestige comparisons with concrete, supportable controls. Consider a concise Security Practices page covering only verified items such as:

- Transport security and security headers
- Server-side form validation and rate limiting
- Hosting isolation
- Patch cadence
- Backup cadence and restoration testing
- Administrative MFA/access practices
- Vulnerability review
- Incident response and notification expectations
- Subprocessors and data flow
- Business-continuity coverage

Do not publish RTO, RPO, response times, testing cadence, insurance, or incident commitments unless the operational process supports them.

### Required comparison

Compare a dedicated security page against distributing short evidence blocks across relevant service pages. A dedicated page is preferable if it can remain accurate and maintained; otherwise use smaller verified claims near purchase decisions.

### Acceptance criteria

- Every security claim maps to a real implemented control or documented process.
- Generic ISC2 verification language is replaced or clarified if direct verification cannot be provided.
- Absolute “Fortune 500” and fear-based breach claims are removed or substantiated.
- Security copy distinguishes confidentiality, platform security, availability targets, and legal obligations rather than blending them.

---

## Priority 9 — Tighten law-firm legal/ethical wording

### Current conflicts

- The law-firm hub says Aspired verifies state-bar advertising compliance before launch.
- The detailed service page says the attorney is responsible for confirming requirements.
- “Privileged intake” overstates the legal status of every pre-engagement website submission.
- “A bar complaint waiting to happen” uses fear rather than precise risk language.

### Suggested fix

- Say that Aspired supports implementation of client-provided bar requirements, disclaimers, and review workflows.
- State that the attorney remains responsible for legal/ethical approval.
- Replace “privileged intake” with “sensitive prospective-client information” or similarly precise language.
- Replace bar-complaint predictions with a factual explanation of confidentiality, reputational, and regulatory risk.

### Required comparison

Claude should research relevant state-bar advertising guidance using primary sources for the jurisdictions explicitly discussed. Do not turn general web copy into legal advice. Compare the suggested wording with a more jurisdiction-neutral formulation.

### Acceptance criteria

- Responsibility for compliance is consistent across all law-firm pages, proposals, and contracts.
- No statement guarantees legal or bar compliance.
- Pre-engagement intake is described accurately.

---

## Priority 10 — Reduce conversion and accessibility friction

### Items to investigate

- Whether phone should remain required on the general contact form.
- Whether the contact-form honeypot is actually exposed to assistive technology. The current template already wraps it in `aria-hidden="true"` and uses `tabindex="-1"`; reproduce with more than one accessibility inspection before changing it.
- Whether marketing-use language in the privacy policy matches actual consent and outreach behavior.
- Whether scheduler submissions can expose fields in a URL when JavaScript fails.
- Whether “Client Login” visually overpowers the prospect CTA in the header.
- Header logo legibility and size across breakpoints.
- The reported About-page typo “Istarted,” if it still exists in the current source or database-rendered output.

### Suggested fixes

- Make phone optional for initial written inquiries unless lead operations require it.
- Keep the honeypot inaccessible to legitimate users while preserving bot detection.
- Add an explicit POST method and safe failure behavior to the scheduler form.
- Make Client Login visually secondary for logged-out marketing visitors.
- Increase the logo/wordmark’s legibility without disturbing header responsiveness.
- Clarify that audit URLs are processed in the session and persisted only when an email report is requested, which matches the current view/model behavior.

### Acceptance criteria

- Forms pass keyboard and screen-reader checks.
- No personal fields can fall into query strings or analytics.
- Privacy wording matches actual persistence and marketing behavior.
- Mobile header, focus states, and CTA hierarchy remain usable.

---

## Priority 11 — Tone and messaging pass

### Preserve

- Directness
- Security-first expertise
- Ownership and no lock-in
- Transparent pricing
- Honest statements about fit and ranking uncertainty
- Founder accessibility

### Reduce or rewrite

- Repeated “hand-coded/no templates” copy when it crowds out buyer outcomes
- “Fortune 500” comparisons
- “Weaponize it”
- “Donations to Meta”
- “Money set on fire”
- Claims that other agencies do not understand threats
- Sweeping Wix/Squarespace ownership/disappearance claims

### Suggested messaging hierarchy

1. Business outcome: more qualified inquiries, clearer conversion, dependable operation.
2. Risk reduction: security-minded implementation, ownership, transparent scope.
3. Delivery method: custom code where it is the right fit; honest advice when a builder is sufficient.
4. Ongoing growth: maintenance, SEO, and social as follow-on services.

### Required comparison

Compare a sitewide rewrite against a smaller pass focused on homepage, pricing, About, law-firm hub, and conversion pages. Prefer the smallest change that produces a coherent voice and avoids SEO regressions.

---

## Implementation sequence

### Release 1 — Truth and trust

1. Verify brand facts.
2. Correct Denis Law Group everywhere.
3. Reconcile guarantee/refund/jurisdiction/cancellation.
4. Reconcile location and timeline language.
5. Fix material typos and broken external links.

### Release 2 — Conversion consistency

1. Standardize the 30-minute strategy-call naming and routing.
2. Remove pre-call cross-sells.
3. Reconcile social plans with database pricing.
4. Fix scheduler failure/privacy behavior.
5. Clarify contact versus booking versus audit paths.

### Release 3 — Evidence and brand polish

1. Add verified case-study evidence.
2. Improve security substantiation.
3. Tighten law-firm wording.
4. Refine tone, logo prominence, founder presentation, and header hierarchy.

Do not combine all three releases into one unreviewable change if they can be safely separated.

---

## Verification checklist

### Automated

- Run `python manage.py check`.
- Run targeted tests for each modified app, likely `core`, `public`, `scheduler`, `billing`, and `clients` as applicable.
- Add regression tests for:
  - No Denis “built by Aspired” claims.
  - Canonical call duration/name and scheduler destinations.
  - One timeline policy across important public pages.
  - Social-plan service copy matching `ServiceTier`/`TierFeature` data.
  - Location/base-versus-service-area wording.
  - Guarantee/refund consistency markers.
  - Scheduler form method and CSRF behavior.
  - No multiline `{# ... #}` template comments.

### Manual browser QA

Test desktop and mobile for:

- Homepage
- Pricing
- About
- Contact
- Canonical scheduler
- Free audit and results
- Portfolio index
- Denis Law Group case study
- Law-firm hub, web-design, and SEO pages
- Digital marketing/social page
- Terms, refund policy, and privacy policy

Verify CTA destinations, keyboard navigation, focus visibility, screen-reader labels, form errors, no-JavaScript behavior, external links, structured data, metadata, and responsive layout.

### Content grep before completion

Search the entire repository and relevant database content for obsolete phrases, including:

- Denis + built/from scratch/hand-coded/page builder/new practice launch
- 15-minute / kickoff call
- 3 weeks / 4 weeks / six weeks
- 30-Day Money-Back Guarantee
- based in San Antonio and Atlanta
- privileged intake / bar complaint
- Fortune 500 / weaponize / donations to Meta
- conflicting social platform counts

Remember that grep alone will not find database-edited production content. Inspect the relevant `ServiceTier`, `TierFeature`, and `CaseStudy` rows.

---

## Required final report from Claude Code

For each priority implemented, provide:

1. What research showed.
2. The suggested fix from this document.
3. Claude’s alternative.
4. Which option was chosen and why.
5. Files, migrations, seed data, and database rows changed.
6. Tests and manual browser checks performed.
7. Remaining owner/legal decisions.
8. Deployment commands needed to update production data safely.

Do not claim the remediation is complete while production seed/database content can still restore the old public claims.
