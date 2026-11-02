# Cold Outreach Pipeline — Scrape → Enrich → Icebreaker

A generic build guide for the lead pipeline: where leads come from, what
gets checked, what gets measured, and how the personalised line gets
written. Nothing here is specific to one industry or territory — swap in
your own niche, geography and offer.

The whole thing is a **queue of independent stages**. A lead moves one
stage at a time, and every stage is re-runnable on its own. That matters
more than it sounds: when a stage fails you re-run that stage, not the
whole pipeline, and you never re-pay for the expensive stages upstream.

---

## The shape of it

```
  [1] SOURCE      B2B contact database (Apify actor)  →  raw contact rows
                        │
  [2] IMPORT      suppression → dedup → score → save
                        │
  [3] VERIFY      role-address screen → bounce check (MillionVerifier)
                        │   ✗ info@, malformed, will-bounce → BLOCKED here
                        │
  [4] ENRICH      homepage scrape · TLS probe · PageSpeed · search fallback
                        │
  [5] VERIFY      again — for sources that had no email until enrichment
                        │
  [6] ICEBREAK    LLM writes ONE true sentence, screened before storage
                        │   ✗ fabricated / critical / off-spec → rejected
                        │
  [7] SEGMENT     assign to a campaign (one niche × geography per campaign)
                        │
  [8] PUSH        → sending platform (Instantly), lead + custom variables
                        │
  ═════════ your app ends here — the sending platform takes over ═════════
                        │
  [9] SEND        multi-step sequence · mailbox rotation · warmup · throttle
```

**Your app is the brain. The sending platform is the mouth.** Your code
decides *who* gets contacted and *what makes them worth a sentence*. It
does not deliver mail. Deliverability — rotation, warmup, throttling,
bounce handling, unsubscribe links — is a solved problem you should rent,
not build. Blasting from one domain with no rotation is how you burn a
domain in a week.

---

## The stack

| Job | Tool | Cost | Notes |
|---|---|---|---|
| Lead sourcing | **Apify** actor (Apollo-style B2B contact DB) | ~$0.02/run + ~$0.002/lead | Person-level, not a Maps scraper — see below |
| Email verification | **MillionVerifier** or **ZeroBounce** | ~$0.001–0.004/address | Interchangeable behind one interface |
| Site performance/SEO | **Google PageSpeed Insights API** | free | Key optional, but rate-limited without one |
| Missing website / socials | **Brave Search API** | free to 2,000/mo, then ~$3/1,000 | Clean JSON; Google CSE and scraping Bing/DDG are dead ends |
| TLS check | Python stdlib `ssl` | free | A real handshake — not an HTTP status |
| Icebreaker copy | **Anthropic Claude API** | ~$0.001–0.01/lead | One short call per lead |
| Sending + sequences | **Instantly** (API v2) | plan-based | Owns the template, warmup, rotation |
| Queue / scheduling | **Celery + Redis** | self-hosted | Every stage is a task |

Everything is behind an env var and degrades to a no-op when the key is
missing, so the pipeline runs on a laptop with zero accounts configured —
it just does less.

```bash
APIFY_TOKEN=
APIFY_LEADS_ACTOR_ID=
APIFY_MONTHLY_BUDGET_USD=5.00
APIFY_MAX_TOTAL_CHARGE_USD=0.50

EMAIL_VERIFY_PROVIDER=millionverifier   # or zerobounce, or blank
EMAIL_VERIFY_API_KEY=
EMAIL_VERIFY_REQUIRED=True              # unverified != sendable
EMAIL_VERIFY_ALLOW_CATCH_ALL=False

GOOGLE_PAGESPEED_API_KEY=
BRAVE_SEARCH_API_KEY=
ANTHROPIC_API_KEY=

INSTANTLY_TOKEN=
INSTANTLY_MAX_PUSH_PER_DAY=200
COMPANY_POSTAL_ADDRESS=                 # CAN-SPAM requires this in the copy
```

---

## [1] SOURCE — where leads come from

**Use a person-level B2B contact database, not a Google Maps scraper.**

This is the single most important decision in the pipeline, and it is the
one most people get wrong.

- A Maps scraper returns a **business**. A business has an `info@` inbox.
  You then have to scrape an email off their homepage, and any prospect
  without a website is unreachable by definition — which is exactly the
  prospect your scoring model ranks highest.
- A contact database returns a **person**: name, job title, seniority, and
  a real work address. That is an *addressable* lead, not merely a
  discovered one.

Measured difference on real runs: a Maps-sourced list was ~34% role
addresses. The contact-DB list was 0 out of 90.

Maps data still has a use — Google rating and review count are good
material for a warm opener — but as an *enrichment join* on leads you
already have, not as the source of the list.

### Filter at the source, not downstream

Every filter you can push into the actor's own query is a lead you never
pay to fetch, verify, personalise, and then reject. In rough order of
value:

1. **Company size** — the single highest-value filter. Without it you will
   fetch enterprises with a thousand staff against an ICP of twenty.
2. **Seniority / job title** — decision-makers only. Emailing a junior
   employee wastes a send and a lead.
3. **Industry (enum, not free text)** — a free-text keyword like
   `"law firm"` also matches companies that *sell to* law firms:
   staffing agencies, marketing consultancies. Use the vendor's exact
   industry enum where one exists.
4. **Vendor-side email validation** — most contact DBs offer a
   "validated email only" flag. Cheaper than paying a verifier
   downstream to discover the same thing.
5. **Location** — check the granularity. Many actors accept country only,
   so state/region has to be enforced after the fetch.

### Spend guards — three of them, because one is never enough

Actors bill per event, and their default result count is often absurd
(one we use defaults to 100,000, which would bill ~$200 against a $5
plan). So:

1. **Runs-per-day cap** in your own settings table.
2. **Month-to-date dollar ledger** — every run writes a row with the
   estimated cost *before* the call and the actual cost after. A run that
   dies mid-flight still consumed compute; costing it only on success
   under-reports exactly when it matters.
3. **Vendor-side ceiling** on the run itself (`maxTotalChargeUsd` in
   Apify). This one holds even when your own arithmetic is wrong.

Always send the result count explicitly. Never rely on an actor's default.

### Two gotchas

- **Free plans often block API-triggered runs.** The actor bills the start
  event anyway and writes a single `{"error": ...}` row to the dataset —
  and the run still reports SUCCEEDED. Detect that shape explicitly, or
  every scheduled scrape looks like "ran fine, found nothing" while
  quietly costing money. The workaround is to trigger the run in the
  vendor's UI and import the resulting dataset by ID; dataset reads are
  free, and everything downstream is identical.
- **A search niche is not a business type.** "family law", "personal
  injury" and "estate planning" are all searches for *law firms*, and the
  campaign that receives them targets one type. Map niche → type once, in
  one function, and derive both the source filter and the downstream
  segment check from that same map. If the two can disagree, they will,
  and the failure is invisible three layers down.

---

## [2] IMPORT — raw rows to saved leads

Per row, in this order:

1. **Suppression check** — unsubscribes and complaints are permanent and
   global. Check before anything else.
2. **Dedup** — exact case-insensitive match on name + city + state, then a
   fuzzy pass (difflib `SequenceMatcher` ≥ 0.8) within the same city and
   state. No unique constraint in the DB; uniqueness is a code-level
   fuzzy decision because "Smith & Co" and "Smith and Company LLC" are one
   business. If there is no location at all, let it through rather than
   risk a false-positive dedup against location-less rows.
3. **Score** — see below.
4. **Save**, then fire enrichment as a background job.

Import returns a summary (`total / imported / duplicates / suppressed /
errors`) that the admin sees immediately, while the slow HTTP work runs
in the queue. A 30-second-per-lead enrichment on the request thread is
how an import page times out.

### Scoring

Pure function, no DB, 0–10, mapped to hot/warm/cold bands. Score on
*severity of the problem you fix*:

| Signal | Points |
|---|---|
| No website at all | 4 |
| PageSpeed < 50 / < 70 / < 85 | 3 / 2 / 1 |
| No Google Business Profile | 2 |
| Zero reviews / under 10 | 2 / 1 |
| No social presence detected | 1 |
| No valid HTTPS | 1 |
| Business email on a free consumer provider | 1 |
| Footer copyright 3+ years stale | 1 |

Missing data scores zero rather than penalising — absence of a
measurement is not evidence of a problem.

Score twice: once at import from whatever the source returned, and again
at the end of enrichment. The second one is the real one. "5 — warm"
becomes "8 — hot" once PageSpeed comes back at 31.

Also ship a `score_breakdown()` that returns the same rules as rows with
signal/points/max — so the admin can see *why* a lead scored what it did.
An unexplainable score gets ignored.

---

## [3] VERIFY — the stage most people skip

This is the stage whose absence kills a campaign silently. Two
independent checks:

### Role-address suppression — free, no vendor, always on

Nobody with authority reads `info@`, and role mailboxes are a well-known
spam-trap pattern, so you damage sender reputation on the way to being
ignored. Build a set of role local-parts (~120 entries: generic, sales,
support, finance, HR, legal, technical, plus vertical-specific shared
inboxes like `intake@`, `scheduling@`, `newpatients@`).

Two implementation details that matter:

- **Match the whole normalised local part, never a substring.** A
  substring rule rejects `sales.director@` and `administer@` — both real
  people worth emailing.
- **Normalise first:** strip `+tags`, collapse separators, so
  `front.desk@`, `front-desk@` and `frontdesk@` are one thing.

Also screen: malformed addresses (scrape artifacts), known disposable
domains, and flag consumer domains (gmail/yahoo/etc.) — those are
*deliverable and often a genuine sole operator*, so don't reject them,
just label them so campaigns can segment.

### Bounce verification — needs a vendor

Above roughly a 3% bounce rate, Google and Microsoft start filtering the
sending domain, and no amount of warming undoes it. At ~1,000 leads/month
this costs under $4. Wrap the vendor behind one function so
MillionVerifier and ZeroBounce are a config change, not a code change.

### Status model

| Status | Sendable | Meaning |
|---|---|---|
| `valid` | yes | vendor confirms the mailbox |
| `consumer` | yes | gmail/yahoo — real person, flagged for segmentation |
| `unverified` | **no** by default | passed the free screen, no vendor configured |
| `risky` | no | catch-all or unknown — accepts at SMTP, bounces later |
| `role` | **never** | shared inbox |
| `invalid` | **never** | malformed, disposable, or bounced |
| `pending` | no | not checked yet |

**Everything ambiguous fails closed.** A wrongly-rejected lead costs one
lead. A wrongly-accepted one costs sender reputation shared across every
future send from that domain. The asymmetry is not close.

Three rules that save you later:

- Run the **free screen before the paid call** — a role address should
  never cost an API credit, and role suppression must keep working on a
  server with no key at all.
- On a **provider outage, leave the status unchanged** so a retry can
  succeed. A transient 500 must not permanently mark a good prospect
  invalid.
- **"No email yet" is not "bad email."** Sources that supply no address
  only get one during enrichment; marking them invalid here kills every
  lead from that source before the stage that finds their address has
  run. That is why verification runs **twice** — once before enrichment
  for sources that ship an address, once after for sources that don't.
  Both passes only look at rows still pending, so it's cheap and
  idempotent.

---

## [4] ENRICH — measure things you're allowed to say

Everything here exists to give the icebreaker **checkable facts**. Each
step is isolated: a failed PageSpeed call must not skip email extraction.
Each step appends a line to a per-lead `enrichment_log` so you can see
why a field is blank without re-running anything.

### Homepage scrape (free)

Fetch the homepage, then — only if no email was found — walk a short list
of likely contact pages (`/contact`, `/about`, `/team`, …) and stop at
the first hit.

Pull out:

- **Email.** Try in order of reliability: `mailto:` hrefs → plain regex →
  Cloudflare-obfuscated blobs (`data-cfemail="..."`, first byte is an XOR
  key) → text obfuscation (`name [at] domain [dot] com`). Decode HTML
  entities *before* any pattern runs — plenty of sites emit
  `info&#64;firm&#46;com`, and against raw markup you find nothing.
  Validate the winner with a **full match**, not a search: `mailto:`
  hrefs carry `?subject=`, trailing punctuation, whole sentences.
- **Social URLs** — first hit per platform, skipping sharer/plugin/login
  URLs, which are not the business's own page.
- **Footer copyright year** — last match on the page wins (the footer
  beats a stray quote up top).
- **Free-provider email flag.**

### TLS — probe it, don't infer it

**Do not set an `has_ssl` flag from "did an HTTPS GET return 200."** That
conflates a TLS failure with a 403 aimed at scrapers and a 404 on a dead
domain. Both of those serve perfectly valid certificates. Get this wrong
and your generator tells a stranger their site "is still running on plain
HTTP" — a false, checkable claim about their business, which is the most
damaging thing this pipeline can emit.

Complete an actual TLS handshake on port 443. Expired, self-signed, or
wrong-hostname certificates count as failures — a browser will refuse
them and a real customer sees a warning page. That's a genuine finding.
Nothing about HTTP status is allowed to influence it.

Related: if HTTPS fails, retry over **HTTP** before declaring the site
dead. Plenty of small-business sites serve fine over http with a broken
cert. "Live site with no usable HTTPS" is a better finding anyway, and
you already recorded it.

### Classify what's actually at the domain

A parked domain is not a bad website — it is the *absence* of one, and
every site-quality observation is meaningless against it. PageSpeed will
happily score a "this domain isn't connected to a site" placeholder
89/100.

Record a `site_status`: `parked` / `unreachable` / `bot_blocked` / live.
Detect parking from status codes plus a marker list (builder
placeholders, "coming soon", "account suspended", default nginx/Apache/IIS
pages, parking services). Then have the icebreaker suppress **every**
site-quality signal when the site isn't real.

**Thin content alone is not parked.** A JS-rendered site serves a nearly
empty shell and fills it client-side — that's an ordinary modern website.
Require corroboration: thin AND no `<script>` AND no real `<title>`.

### PageSpeed Insights

Request the **mobile** strategy — it's what real users get. Pull
performance / SEO / best-practices as 0–100, plus the first few failing
audits for the lead detail page.

Set the timeout to ~30s, not 60. A successful call returns in 10–20s;
anything past 30 has already failed, and on a 95-lead batch that extra
wait was most of the runtime. PageSpeed is also the most expendable
signal — a missing score just means the opener leans on something else.

### Search fallback for leads with no website

Only fires when the website field is still blank. Cap it at ~3 queries
per lead (site / Facebook / Instagram) so one batch can't burn a monthly
free tier, and sleep ~1.1s between them for the rate limit. If it finds a
site, recurse into the homepage scrape and PageSpeed so the lead ends up
with the same data as any other.

**Verify every hit before you store it.** Taking the first
`facebook.com` result on faith was wrong about **50% of the time**,
measured against phone-verifiable pairs:

```
Godwin Law Office   →  Goodwin & Goodwin, LLP | Charleston
Family Vital PC     →  Vital Interaction | Austin
```

Three tests, cheapest and most reliable first:

1. **Phone** — same last 10 digits in the result text is near-certain. And
   a result showing a *different* phone for a named business is positive
   evidence **against**, not merely absent evidence.
2. **Distinctive name tokens** — every identifying word present in the
   title, after stripping generic tokens (`law`, `office`, `group`, `llc`,
   `dental`, `clinic`, `the`, `and`…) so "Law Office of X" and "X Law"
   compare as the same business.
3. **Fuzzy similarity** ≥ 0.65 as a fallback for spelling drift.

Log the accept/reject reason. A wrong social URL isn't cosmetic — it
feeds the scorer, and it feeds the copy generator as a "verified
observation", which is how an invented fact reaches a prospect.

---

## [5] ICEBREAK — one sentence, not a whole email

### Why one sentence

Early versions had the LLM write the entire email per lead. That makes
every send a separate LLM call, makes A/B testing meaningless (no two
emails share a control), and puts your guardrails in the position of
policing free-form text on every single send.

Split it:

- **The sending platform holds the template** — the constant, the thing
  being tested.
- **Your app writes `{{icebreaker}}`** — the variable, one sentence per
  lead.

One sentence is also the only part a recipient reads as personal. The
rest of a cold email is structurally identical no matter who gets it, and
pretending otherwise costs tokens without buying anything.

### Warm, not critical

The obvious first draft opens with a defect: *"Your PageSpeed is
36/100."* It's specific and it proves you looked — and it tells a
stranger their work is bad in sentence one. Nobody replies warmly to
that.

Split the material:

- **The opener draws on facts about the business** — years in operation,
  what they do, where they are, review volume. Research reads as respect.
- **Site findings feed the offer, further down** — that's where a problem
  belongs, because there it arrives attached to a free fix instead of a
  criticism.

One subtlety on reviews: quoting a mediocre star rating back at someone
is a criticism wearing a statistic. Strong rating → cite rating and
count. High volume, ordinary rating → cite the **count only**, never the
stars. Weak → say nothing and let another fact carry the line.

### Prompt design

Feed the model **only measured facts** — a compact block of stored fields
plus a "verified observations" list generated from enrichment. Tell it
explicitly that thin data is normal and **a slightly generic line is
correct** when the data is thin. Without that, a model with nothing to
say will invent something.

Rules in the system prompt: one or two sentences, under ~35 words, no
greeting, no sign-off, no markdown, no quotes, plain ASCII punctuation
(em-dashes and curly quotes read as machine-written, and cold email is
exactly where that suspicion costs a reply), never criticise, never
invent, never mention price, output the line and nothing else.

### The screen — this is the part that matters

**The prompt is a request; the screen is the enforcement.** Storage *is*
approval here — anything you save goes out to a real person as a merge
variable. So screen before storing, and reject rather than repair:

- **Cites a score never measured** — any `NN/100` must match a real
  measurement on that lead.
- **Cites a year you don't hold** — "practising since 1998" is exactly the
  flattering detail a model invents, and it's disprovable in one second.
  Any 4-digit year must be a year actually recorded for the lead.
- **Unverifiable claims** — "I read your recent…", "congratulations on
  your award", "we worked with a client who…", "37% more leads", "last
  week".
- **Model commentary** — "Here's a great opening line:", "As an AI",
  "Could you provide…".
- **Criticism** — a marker list (`slow`, `outdated`, `broken`, `not
  secure`, `missing`, `lacks`, `falling behind`…). Enforced, not
  requested, because it's the whole point of the design.
- **Pricing claims** — checked against your real price list.
- Length and paragraph breaks.

**A rejected line means the lead waits.** It does not go out generic,
because the push stage refuses any lead with no icebreaker. An invented
detail is worse than a bland one: it's instantly checkable, and being
caught fabricating ends the conversation permanently.

Only generate for leads that are **verified sendable AND finished
enrichment**. An icebreaker written before the measurements exist has
nothing specific to say, and it costs a real API call to find that out.

---

## [6–8] Segment, push, send

- **One niche × one geography per campaign.** Copy that references
  something specific can't be written for a blended list, and
  per-campaign reply rate is the only way to learn which segment
  actually wants this. Keep the number of arms small — every arm is a
  campaign someone has to hand-build, and splitting the list N ways
  divides your statistical power by N.
- **The A/B variable should be the offer, not the city.** The offer moves
  reply rate far more than wording does.
- **Re-check every gate at push time.** The push function should not
  trust the query that selected the leads — it is the last thing standing
  between an address and a real send. Refuse: unsendable status,
  suppressed, unsubscribed, already pushed, no icebreaker, segment
  mismatch, over the daily cap.
- **Never let code activate a campaign.** Build it paused; starting it is
  a deliberate human click. That's the irreversible step that puts mail
  in front of real people.
- Custom variables that reach the template: `{{icebreaker}}` plus city,
  state, website, business type, and the platform's own first-name /
  company-name merges.

### On the sending side

Plain text for cold email. No HTML, no images, no tracking pixel on touch
one. Cap the sequence at ~4 touches, stopping on any reply. Warm new
mailboxes for 2–3 weeks before the first real send — this is the longest
lead-time item in the whole project, so start it on day one; it blocks
nothing else. Spread sending across multiple mailboxes on multiple
domains, and never send from a domain whose name doesn't match the offer
in the email — a pitch for X arriving from a Y-branded domain reads as
bait-and-switch, and the complaints burn the wrong brand.

CAN-SPAM: working opt-out and a valid physical postal address in every
commercial email. Refuse to generate copy while that address is unset.

### Getting replies back

If your platform gates outbound webhooks behind a higher plan, **poll the
inbox instead** — it's usually the better option anyway. No public
endpoint to secure, no shared secret to leak, and delivery is
exactly-once by construction rather than at-least-once against a hostile
internet. The cost is latency, and nobody expects a cold email answered
in ninety seconds.

Two things to handle if you poll:

- **Bounces arrive as mail, not as events.** Classify each message as a
  bounce *before* the reply filter sees it, or a `mailer-daemon@` notice
  gets discarded as "automated sender" and the dead address keeps
  receiving mail. Match the failed address against leads you actually
  pushed, rather than taking the first address in the notice — otherwise
  you'll suppress `postmaster@`, your own sending mailbox, or a support
  URL.
- **Filter your own mail out.** Drop messages from your own domains,
  automated mailboxes, out-of-office autoresponders, and addresses you
  never emailed. Run this filter **even when the sender matches a lead
  row** — you will eventually have a lead row for one of your own
  companies, and gating the filter behind "did we find a lead" rebuilds
  the bug exactly.

---

## Orchestration

Every stage is its own background task, so a failure in one doesn't roll
back the stage before it and any stage can be re-run alone.

| Task | Does | Cost |
|---|---|---|
| `verify_leads` | screen + verify pending leads | ~free |
| `enrich_pending_leads` | homepage, TLS, PageSpeed, search | free, slow |
| `generate_icebreakers` | one line per sendable enriched lead | LLM call/lead |
| `assign_campaigns` | place ready leads into an arm | free |
| `push_to_platform` | push ready leads to their campaign | free |
| `run_pipeline` | all of the above, in order — one schedule entry | — |

**Sourcing is deliberately NOT in the pipeline task.** It costs money per
run and belongs on its own schedule, so a pipeline retry can never
re-trigger a paid scrape.

Order is not arbitrary:

```
verify(pre) → enrich → verify(post) → [paid data joins] → icebreaker
    → assign → push
```

Cheap filters before expensive ones, always. Verification before
icebreaker generation because a role address is worth zero LLM calls. Any
paid per-lead lookup after verification, so you only spend it on leads
already known to be contactable.

Ship a **status command** that prints where the funnel currently stops —
counts by stage, plus the reason for the first stage that has nothing
ready. Without it, "nothing is sending" is a two-hour investigation every
time, and the answer is nearly always one unset env var.

---

## The five things that actually decide whether this works

1. **Source people, not businesses.** Everything downstream inherits the
   quality of this decision.
2. **Verify before you spend.** Both money (API credits, LLM calls) and
   the thing you can't buy back — sender reputation.
3. **Measure, don't infer.** Every claim in the copy must trace to a
   stored field you actually measured. TLS by handshake, not by HTTP
   status.
4. **Screen the output, not just the prompt.** The prompt is a request.
   The screen is enforcement, and storage is approval.
5. **Rent deliverability.** Warmup, rotation and throttling are a solved
   problem. Building it yourself is how you burn a domain.

The recurring theme is **failing closed**: every ambiguous case resolves
to "don't send" or "this lead waits". One skipped lead costs one lead.
One bad send costs every future send from that domain.
