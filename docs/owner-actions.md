# Owner actions: aspiredwebsites.com implementation plan (Sept 2026)

Things that need you, not code. Everything the site can say without them already ships; each item below unlocks a hidden section or fixes something off-site.

**Where to fill in site content:** Admin dashboard (v2) → **Site Content** (`/admin-dashboard/v2/site-content/`). Saving publishes immediately. Every section stays hidden until its fields are filled, so only write what's true today.

## 1. Answers that unlock hidden sections (Site Content page)

- [ ] **Build scope (D-07):** what the $2,000 build includes (pages, copywriting, photos, logo). One item per line. Shows "What the build includes" on `/pricing/`.
- [ ] **Review automation mechanics (D-08):** job systems it connects to, the no-software trigger, SMS or email, who pays message costs, **consent model**, **opt-out**, multi-location routing, setup steps, live at launch, reporting, and a real sample message. Shows "How It Connects" on `/services/review-automation/`. Never list a platform you don't support today.
- [ ] **Continuity (D-16):** backup cadence and outage-response target. Adds two lines to "How We Support You" on `/about/`.
- [ ] **Founding-client offer (D-17a):** decide whether to offer one. If yes, write the headline, what they get and what you ask, then tick "enabled".
- [ ] **Demonstration HVAC build (D-17b):** only once a real demo site exists. It's always labelled "not a client site".
- [ ] **CISSP member number (D-22):** until it's entered, `/about/` links to the (ISC)² verification tool and says the number is available on request.
- [ ] **Audit data retention (days):** optional. Adds a deletion period to the privacy policy's audit paragraph.
- [ ] **Policy FAQs (M-2.14):** finance the build + $45 plan? Carry over old URLs? Your time commitment? Travel fee? Pause the Full Plan? Each question appears on `/pricing/` only once answered.

Already filled for you (from your 2026-09-25 answers): Google reviews link `https://g.co/kgs/q5dqyQ1`, and "the 210 number takes texts".

## 2. Decisions with a default already live (change if you disagree)

- **Guarantee:** within 30 days of signing, 75% of *everything paid under the agreement* is refunded and 25% kept, for both pay-in-full and installment. Stated on `/pricing/`, Terms, Refund Policy, and in new contracts. The refund is issued from Admin → Website → Billing → 30-day guarantee.
- **Monthly security report:** included in every plan, including Hosting + Security ($45).
- **Session replay on aspiredwebsites.com:** kept and now disclosed in the privacy policy. It never records login, password, portal, onboarding or payment pages, and skips any browser sending Global Privacy Control. Recordings are kept 30 days (the existing retention job).
- **Booking calendar hours:** unchanged. They come from Admin → Availability (Eastern time). The live calendar offers Saturdays; turn that off there if you don't take weekend calls (plan D-24).
- **Contact form:** kept short (name, phone, email, message), per your answer. A two-field "call me back" form was added above it; requests arrive by email only.
- **Instantly campaign schedule** (`outreach/instantly.py`): still 9–5 **Central**. Instantly's API only accepts its own timezone list, and changing it blind could reject campaign creation. Confirm the Eastern value Instantly accepts, then change that one line.

## 3. Off-site (not done by Claude; no verified access)

- [ ] **aspiredwebsiteswp.com:** still live, selling WordPress builds at $80/$150/$220/mo, SEO and social media, with San Antonio and Atlanta locations. It contradicts "we don't build on WordPress", current pricing and the location. You have a client subscribed there, so plan the move: migrate that client, then 301 the whole domain to `https://aspiredwebsites.com/` (minimum: noindex every page and remove the pricing and services pages). Then update the Facebook page, the Instagram bio ("we even offer digital marketing!") and X.
- [ ] **Google Search Console:** submit the regenerated sitemap. Request recrawl of stale `www.` snippets. After a few weeks, check coverage for `/portfolio/other/`, the law-firm post and the legacy URLs.
- [ ] **Google Business Profile:** confirm `https://g.co/kgs/q5dqyQ1` is Aspired's. Service area should say Warner Robins, and categories and description shouldn't mention SEO or social. The site's structured data no longer claims 24/7 opening hours, so make the GBP hours match reality.
- [ ] **Client sites (Phase 8, each needs the client's OK):**
  - denislawgroup.com: malformed Instagram link `https://%20instagram.com/DenisLawGroup`.
  - burglandtech.com: service cards rely on hover (not tappable on a phone); the footer copyright links to `/login`.
  - foodtrucksofsa.com: footer phone reads "Coming Soon".
- [ ] **Burgland Technologies city:** the case study has no location. Add it in the admin if you want a city tag.
- [ ] **Phone-width screenshots for case studies (M-5.08):** not added. The model holds one screenshot per study. Say the word and a mobile-screenshot field plus capture script can be added.

## 4. Server-side notes from this rollout

- Both droplets now run on **America/New_York** (system clock and Django `TIME_ZONE`). Celery beat times are Eastern.
- Monthly security report: file-integrity checks need an automation-enabled SSH credential for each site in the vault. After a planned deploy on a client server, click "Accept current state as new baseline" on its droplet-check page, or the next report will flag the changed files. The worker host needs nmap, nikto and wpscan.
