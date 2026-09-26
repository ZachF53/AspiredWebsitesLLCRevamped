# aspiredwebsites.com — Master Implementation Guide

**Started:** 2026-09-25 (ET) · **Owner:** Zachery Long · **Executor:** Claude Code
**Source plan:** `aspiredwebsites-master-implementation-plan.md` (merged GPT + Opus + Fable) — task IDs `M-x.xx` below refer to it.
**Rule of this file:** every task is ticked `[x]` only after it is committed. Status legend: `[x]` done · `[~]` partial / adapted (see note) · `[-]` skipped on purpose (see note) · `[ ]` open.

---

## A. What changed between the first plan (FABLE) and the merged plan

1. **Re-checked against the live site.** It marks as already done: the new `/about/` (with photo), the Warner Robins "From $2,000" line, the removed local-SEO links on location pages, the `/for-law-firms/` 301, the honeypot, the security headers and the calendar. FABLE tasks that would have overwritten those fixes were dropped (T-1.03, T-1.05, T-3.01 rebuild, T-3.02 dropdown edits, T-5.09).
2. **`/for-law-firms/` redirect target** stays `/services/web-design/` (FABLE said `/`).
3. **New tasks:**
   - CISSP verify link is a 404 (M-1.03).
   - `/about/` still says "three to four weeks" (M-1.07).
   - **Session replay is undisclosed** (M-1.08).
   - The "security report in every plan" claim is inconsistent (M-1.09).
   - FAQ answers for buyers' unanswered questions (M-2.14).
   - Sell the security report consistently (M-2.15).
   - `robots.txt` lists the back-office routes (M-3.07).
   - JSON-LD residue: "Local SEO", old logo, 24/7 hours, Central-time dates (M-3.08).
   - Booking timezone label, format label and real hours (M-4.04).
   - Contact-form qualifiers (M-4.06).
   - Review-request consent and opt-out fields (M-5.06).
   - Multi-location FAQ (M-5.11).
   - Chilton portfolio entry (M-5.12).
   - Hide the server version and clean Permissions-Policy noise (M-6.08).
   - Performance budget (M-6.09).
   - Insights ordering (M-6.10).
4. **New owner decisions** D-21 to D-25: session replay, CISSP verification, whether the $45 plan gets the security report, real call hours, `TIME_ZONE`.
5. **Grep gate expanded** with first-person-voice and placeholder checks, because new copy was regressing to "I".

## B. Owner answers (2026-09-25) and how they change the plan

| # | Answer | Effect |
|---|---|---|
| 1 | Treat the plan as a checklist | Skip what's done; keep existing redirects to `/services/web-design/`; only `local-seo` → `/services/review-automation/` changes |
| 2 | Keep the contact form short; remove the two legacy build tiers | M-4.06 qualifiers **skipped** (only the 210 note and Warner Robins added); Essential/Premium removed everywhere |
| 3 | Admin-editable settings record for owner inputs | New `SiteContent` singleton (public app), edited in the admin dashboard, replaces the plan's `content/*.yml` files |
| 4 | Extend the Django brand test instead of a bash script | Grep gate = `public/tests_brand_consistency.py` + `manage.py content_gate` (checks live rendered HTML) |
| 5 | Update DB content however is best | Prod DB holds the content (some edited in admin). Idempotent **data migrations** do targeted replacements, and the seed commands are updated too |
| 6 | Whitehead Wellness exists on prod | Added to `seed_case_studies` from the prod row |
| 7 | Deactivate tiers; check prod | Prod has 10 client profiles (7 `essential_build`, 2 `premium_build`) and **no active Stripe subscriptions**. Old package choices stay for those records; tiers are deactivated |
| 8 | Build the backend to match | New `hvac_*` packages; booking → package mapping; CRM prefill |
| 9 | Update CLAUDE.md | Done at the end |
| 10 | No 50% anymore | Pay in full or 24 installments. **Checkout for this was never wired**, so it's being built (payments track below) |
| 11 | Full Plan billing starts at account creation + contract signing | Charged at signing |
| 12 | 30-day guarantee, but 25% of what's paid is kept | New refund logic + admin action; copy says "75% of what you've paid is refunded" |
| 13 | Hosting $45 **monthly** | Terms/Refund fixed; $45/mo self-checkout built |
| 14 | Unlimited updates: yes. Monthly security audits: yes — **finish the automation** | Security-report track below |
| 15 | 24h / 7 days is true | Kept |
| 16 | Audit ≈ 25 s; remove the AI wording | "about 30 seconds"; AI wording removed from the audit page and results |
| 17 | The 210 number takes texts | "Call or text" |
| 18 | Callback alert by email only | No Twilio |
| 19 | GBP link `https://g.co/kgs/q5dqyQ1` confirmed | Google-reviews link published |
| 20 | Headshot yes; CISSP number later; Chilton is out of business (keep his review); link Moonieful | CISSP links to the ISC2 tool with "member number on request"; no Chilton portfolio entry (M-5.12 skipped) |
| 21 | Owner-input sections stay hidden | D-07, D-08, D-16, D-17 render nothing until filled |
| 22 | Commit granularity: my call | One commit per phase or logical track; task IDs listed in each message |
| 23 | Run straight through; deploy to staging **and prod** | Explicit prod authorization for this job |
| 24 | Baseline + save every page | Done (`docs/baseline/`) |
| 25 | WP site: owner action only (a client is subscribed there) | Listed in `docs/owner-actions.md` |
| + | Server and code run on New York time | `TIME_ZONE = 'America/New_York'`; both droplets' system clocks set to America/New_York; Chicago hardcodes fixed |

## C. How it's being done (architecture)

- **Templates** (`public/templates/public/*`, `core/templates/*`) are edited directly. Every edit is pre-flighted for multi-line `{# #}`.
- **DB content** (CaseStudy, Article, City, TierFeature) is changed by data migrations in `public/migrations` using guarded `str.replace` that applies only if the old text exists, so it's idempotent and safe on prod-edited rows. Seeds are updated so fresh installs match.
- **Owner inputs** go in a `SiteContent` singleton (`public/models.py`) with blank defaults, edited at `/admin-dashboard/site-content/`. Templates render a section only when its field is filled.
- **Grep gate:** `manage.py content_gate [--base-url URL]` renders every public URL (test client by default, or a live URL) and fails on forbidden strings or regexes outside the whitelist. It's wired into the tests.
- **Backend tracks run in parallel in isolated git worktrees** and are merged and reviewed before deploy:
  - **Payments track:** pay-in-full/installment contracts, SubscriptionSchedules ($105×24; $250×24 → $145), $45 hosting checkout, the 75% guarantee refund action, 50% copy removed from the portal and emails, booking dropdown → packages, tier deactivation.
  - **Security-report track:** plan-based recipients, domain-only scanning, pre-report scans, file-integrity baselines, uptime section, auto-send fix, `send_security_summaries` command.
- **Deploy:** staging first (smoke test, content gate against the staging URL), then prod (explicitly authorized). Standard script plus `collectstatic`. Server timezone set via `timedatectl`.

---

## D. Checklist

### Phase 0 — Discovery and baseline
- [x] M-0.01 Repo mapped (findings in this file, §C, and the agent reports)
- [x] M-0.02 Old template confirmed gone (the only `#1A1A1A`/`logo.png` user is the portal base plus the login/password-reset inline logo → M-6.03)
- [x] M-0.03 Baseline: `docs/baseline/html/` (27 pages), `docs/baseline/README.md` (redirects, headers, robots), `docs/baseline/lighthouse/*.json`, `docs/baseline/db/prod_content.json`
  - Lighthouse mobile (prod, before): home 58 / LCP 4.9 s / TBT 1,180 ms · pricing 63 / 4.8 s · portfolio/other 77 / 5.4 s · san-antonio 71 / 5.1 s · schedule 65 / 4.7 s. **"About 1.5 s on mobile, measured" is false** → M-6.09
- [x] M-0.04 Content gate (`content_gate` command + test)
- [x] M-0.05 `SiteContent` singleton + admin editor + `docs/owner-actions.md`

### Phase 1 — Stop the bleeding
- [ ] M-1.01 Booking dropdown: six options, handler, emails, CRM, packages *(payments track)*
- [x] M-1.02 Denis Law Group internal note removed
- [x] M-1.03 CISSP link → `https://my.isc2.org/s/MemberVerification` (member number on request)
- [x] M-1.04 Portfolio + homepage honest ("Recent Work", no "coming soon", real projects)
- [x] M-1.05 Ownership copy qualified ("once paid off")
- [x] M-1.06 Refund Policy in the footer
- [x] M-1.07 `/about/` timeline 4–6 weeks
- [x] M-1.08 Session replay disclosed; GPC honoured; no recorder on auth/token routes
- [x] M-1.09 Security report "every plan" made consistent (owner: included in every plan → added to the $45 card)

### Phase 2 — One pricing truth
- [x] M-2.01 Terms §2 services
- [x] M-2.02 Terms §3 payment (full/installment, monthly hosting, 75% guarantee)
- [x] M-2.03 Terms §4/§6 (content clause, no "tiers", final files at payoff)
- [x] M-2.04 Terms/Privacy address → Warner Robins, Georgia
- [x] M-2.05 Refund Policy rewrite (no social/annual/50%; 75% guarantee)
- [x] M-2.06 "How billing works" on `/pricing/`
- [x] M-2.07 "The Fine Print, In Plain English" on `/pricing/`
- [x] M-2.08 Total-cost table (computed from ServiceTier)
- [x] M-2.09 Build scope, gated (SiteContent)
- [x] M-2.10 Full Plan cards renamed; hosting button below features
- [x] M-2.11 Cost article contradictions
- [x] M-2.12 Timeline 4–6 weeks everywhere (+ `site_facts`)
- [x] M-2.13 Missed-payment sentence
- [x] M-2.14 FAQ additions (true answers only)
- [x] M-2.15 Security report sold consistently
- [ ] Backend: pay-in-full / installment contracts and checkout, $45 hosting checkout, guarantee refund *(payments track)*

### Phase 3 — Legacy purge
- [x] M-3.01 `/about/` residuals (Warner Robins, continuity block, "About" link)
- [x] M-3.02 Titles (`/insights/`, `/contact/`)
- [x] M-3.03 Residual SEO/social/law/FindLaw/WordPress strings
- [x] M-3.04 Redirects (local-seo → review-automation; `/portfolio/other/` → `/portfolio/`; root icon aliases)
- [x] M-3.05 Law-firm post noindex + delisted + notice
- [x] M-3.06 Continuity statement component
- [x] M-3.07 robots.txt trimmed + `X-Robots-Tag` on back-office routes
- [x] M-3.08 JSON-LD cleanup + `TIME_ZONE = America/New_York` (+ server TZ)
- [ ] M-3.09 Grep gate passes

### Phase 4 — Conversion path
- [x] M-4.01 Tap-to-call in the header on every page
- [x] M-4.02 Homepage trust line
- [x] M-4.03 Start CTAs → `/design/schedule/`
- [x] M-4.04 Booking page: call-instead line, timezone label, format label, what happens next, metadata. Real hours: availability is already DB-driven (AvailabilityWindow, ET) → no change to your configured hours
- [x] M-4.05 Call-me-back form (email alert only)
- [-] M-4.06 Contact-form qualifiers — **skipped per owner (keep it short)**; only the 210 note + Warner Robins
- [x] M-4.07 Audit copy (30 s, no AI wording, data statement matches privacy)
- [x] M-4.08 Pricing FAQs → `<details>`

### Phase 5 — Proof and audience
- [x] M-5.01 `/portfolio/other/` merged into `/portfolio/`; 301
- [x] M-5.02 Founding-client + demo components (hidden)
- [x] M-5.03 Trade-welcome lines
- [x] M-5.04 "Who's behind this" trust block on locations + posts
- [x] M-5.05 "Just starting out?" + jargon glosses
- [x] M-5.06 Review automation "How it connects" (gated, with consent/opt-out)
- [x] M-5.07 Google reviews link + Moonieful link
- [~] M-5.08 Case-study upgrades (double `<hr>`, "What We Built", Denis tag, Burgland city) — double <hr> removed, "At a Glance" instead of "Results", Denis tagged; Burgland city + phone screenshots → owner-actions
- [x] M-5.09 Voice sweep + regex in the gate
- [x] M-5.10 Location pages parallel
- [x] M-5.11 Multi-location FAQ (page not built — owner opt-in)
- [-] M-5.12 Chilton portfolio entry — **skipped: business closed** (testimonial kept, unlinked)

### Phase 6 — Hygiene
- [~] M-6.01 Metadata (unique og:image:alt, schedule/password-reset titles) — og:image:alt now neutral ("Aspired Websites logo"); schedule + password-reset titles done
- [x] M-6.02 Footer standardized
- [x] M-6.03 Duplicate logo on login/password reset
- [x] M-6.04 "See More Work" links
- [x] M-6.05 Privacy accuracy (session replay, audit data, review requests, effective date)
- [x] M-6.06 Sitemap
- [ ] M-6.07 www → apex verified
- [~] M-6.08 `server_tokens off`, Permissions-Policy cleanup, root icons, manifest MIME — Permissions-Policy cleaned, root icon 301s in code; nginx server_tokens + manifest MIME at deploy
- [~] M-6.09 Performance: defer gtag/tracker/rrweb; speed claim matches the measurement — gtag + recorder deferred to after load; re-measure at deploy; numeric speed claims removed until re-measured
- [x] M-6.10 Insights order

### Security-report automation (owner answer 14)
- [x] Plan-based recipients (`reporting/security_eligibility.py`) · domain-only scans for sites off our servers · pre-report scan sweep on the 26th–28th (2am ET) · file-integrity baselines in the droplet health audit (+ "accept new baseline" admin action) · uptime section · per-scan auto-send fixed (`scan.website_new`) · `manage.py send_security_summaries [--dry-run] [--month] [--website] [--resend]` · 217 reporting tests pass
  - Remaining for you: vault SSH credentials per client server (needed for file integrity); nmap/nikto/wpscan on the worker host; WordPress file integrity only runs once those sites are on our servers

### Phase 7 — Verification + deploy
- [ ] Merge the tracks; `manage.py check`; targeted tests
- [ ] CLAUDE.md updated
- [ ] Staging deploy + smoke test + content gate
- [ ] Prod deploy + smoke test + content gate + Lighthouse after
- [ ] `docs/verification/2026-09-26.md`

### Phase 8 — Off-site (owner)
- [ ] `docs/owner-actions.md` written (WP site, GSC, GBP hours, CISSP number, client-site tickets)

---

## E. Assumptions / deviations log
1. **Guarantee 25% retention applies to both pay-in-full and installment builds**, and to everything paid under the agreement (build + plan months) inside the 30 days.
2. **Security report "every plan" includes Hosting + Security ($45)** (owner said monthly audits are included; the $45 card now lists it).
3. **Instantly campaign timezone left at America/Chicago**: its API only accepts its own timezone list; changing it blind could break campaign creation (see owner-actions).
4. **GA4 stays on login/payment pages** (approved 2026-08-02); only our tracker and session recorder are excluded there. GA4 is skipped entirely when a browser sends Global Privacy Control.
5. **Booking availability not changed**: hours are owner-configured in Admin → Availability (Eastern); the plan's Mon–Fri 9–5 default was not forced over your settings.
6. **Contact-form qualifiers (M-4.06) skipped** per owner answer 2; only the 210 note was added.
7. **Numeric speed claims removed** ("under two seconds", "about 1.5 seconds on mobile, measured", "4.1 s to 1.5 s") because the Sept 2026 Lighthouse baseline measured mobile LCP around 4.7–5.4 s; re-add a number only after re-measuring.
8. **Article bodies are exempt from the first-person voice check** (named-author posts); the rule applies to site copy.
9. **Case-study phone screenshots (M-5.08)** not done: the model has one screenshot field; noted in owner-actions.
10. **Chilton testimonial kept, unlinked** (business closed); no portfolio entry (M-5.12 skipped).

## F. Errors / reverts log
- Pre-existing, not caused by this work: `public.tests_audit_sequence` (4 tests) fails locally because `COMPANY_POSTAL_ADDRESS` is not set in the local `.env` (confirmed failing on the untouched base commit).
