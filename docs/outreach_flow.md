# Cold Outreach — How It Works, Start to Finish

Built 2026-08-22. Replaces the SendGrid cold-email path that sent 416
emails and produced zero human replies.

Run `python manage.py outreach_status` at any point to see exactly where
the funnel currently stops.

---

## The shape of it

```
  [1] SOURCE      Apify person-level DB  →  raw contact rows
                        │
  [2] IMPORT      suppression → dedup → score  →  Lead (status=new)
                        │
  [3] VERIFY      role suppression → bounce check
                        │   ✗ info@, office@, malformed, bounced → BLOCKED
                        │
  [4] ENRICH      PageSpeed · HTTPS · copyright year · socials
                        │
  [5] ICEBREAK    Claude writes ONE true sentence about this business
                        │   ✗ fabrication / commentary → rejected, lead waits
                        │
  [6] SEGMENT     assign to an OutreachCampaign (niche × state)
                        │
  [7] PUSH        → Instantly, lead + custom variables
                        │
  ════════════════ Django ends here. Instantly takes over. ════════════════
                        │
  [8] SEND        4-step sequence · mailbox rotation · warmup · throttle
                        │
  [9] POLL        Django polls the unibox every 15 min (no webhook plan)
                        │   ✗ own-domain, autoresponder, no-reply → dropped
                        │
  [10] CLASSIFY   Claude classifies the reply, drafts a response
                        │
  [11] BOOK       you approve → call booked
```

**Django is the brain. Instantly is the mouth.** Django decides who gets
contacted and what makes this business worth a sentence. Instantly owns
everything about actually delivering mail — rotation, warmup, throttling,
bounce handling, unsubscribe links. That division is the point: 416 emails
from one domain with no rotation is why the old path could never scale
past useless.

---

## Stage by stage

### [1] SOURCE — `outreach/apify_source.py`

Person-level B2B database, **not** a Maps scraper. That difference is the
whole fix: Maps returns a *business*, and a business has an `info@`
inbox. The Apify actor returns a *named person* with a real work address.

Verified against 90 live rows: **0 role addresses out of 90.** The same
screen run against prod's Maps-sourced leads blocks 34%.

- Actor: `code_crafter/leads-finder` (`APIFY_LEADS_ACTOR_ID`)
- Cost: $0.02/run start + $0.002/lead → 50 leads ≈ $0.12
- Budget: `$5/month` free plan, three independent guards in the module

**Known blocker:** the actor refuses API-triggered runs on the free Apify
plan. It bills the $0.02 anyway and returns one `{"error": ...}` row.
`_raise_if_actor_refused()` detects this. Two ways forward:

| Path | How | Cost |
|---|---|---|
| **Free** (start here) | Run in Apify's UI, paste the dataset id into `import_from_dataset()` | $0 extra |
| **Paid** | Apify Starter — unlocks API runs, needed for Prospect to source unattended | ~$49/mo |

Standing searches live in `ScrapeJob` and run daily at 02:00.

### [2] IMPORT — `outreach/pipeline.py`

`import_leads()` — suppression check → fuzzy dedup on firm+city+state →
score → create `Lead` → fire enrichment in Celery. Unchanged; it already
worked.

### [3] VERIFY — `outreach/verify.py` ← **the stage that did not exist**

This is the root-cause fix. Two independent checks:

**Role suppression** — free, no vendor, always on. Matches the *whole
normalised local part*, never a substring, so `sales.director@` and
`administer@` survive while `sales@` and `admin@` do not. `+tags` and
separator spellings (`no-reply` / `no.reply` / `noreply`) collapse to one
thing.

**Bounce verification** — needs an API key. Above a 3% bounce rate Google
and Microsoft start filtering the domain and no amount of warming undoes
it. ~$0.001–0.004/address; under $4/month at 1,000 leads.

```bash
EMAIL_VERIFY_PROVIDER=millionverifier   # or zerobounce, or blank
EMAIL_VERIFY_API_KEY=...
EMAIL_VERIFY_REQUIRED=True              # unverified ≠ sendable
EMAIL_VERIFY_ALLOW_CATCH_ALL=False      # catch-all bounces later
```

Statuses and what they mean:

| Status | Sendable | Meaning |
|---|---|---|
| `valid` | yes | vendor confirms the mailbox |
| `consumer` | yes | gmail/yahoo — a real sole practitioner, flagged for segmentation |
| `unverified` | **no** by default | passed the free screen, no vendor configured |
| `risky` | no | catch-all or unknown — accepts at SMTP, bounces later |
| `role` | **never** | `info@`, `office@`, … |
| `invalid` | **never** | malformed, disposable, or bounced |
| `pending` | no | not checked yet |

**Everything ambiguous fails closed.** A wrongly-rejected lead costs one
lead. A wrongly-accepted one costs sender reputation shared across every
future send.

> With no vendor key set, everything that passes the free screen is
> `unverified`, which is **not sendable**. That is deliberate — but it
> means the funnel is blocked at this stage until you either set a key or
> explicitly set `EMAIL_VERIFY_REQUIRED=False`.

### [4] ENRICH — `outreach/enricher.py`

PageSpeed, HTTPS reachability, footer copyright year, social URLs. These
are the facts stage 5 is allowed to use.

**Fixed this build:** the Brave-search fallback used to take the first
`facebook.com` hit with no verification. Measured wrong ~50% of the time:

```
Godwin Law Office   →  Goodwin & Goodwin, LLP | Charleston
Gonzalez Raul A     →  raul.gonzalez@traviscountytx.gov
Family Vital PC     →  Vital Interaction | Austin
```

Every hit now runs `_matches_business()`: phone match (last 10 digits) →
distinctive-token match → fuzzy similarity ≥ 0.65. A hit showing a
*different* phone is treated as positive evidence against, not merely
absent evidence. Rejections are written to `enrichment_log` with the
reason.

**Also fixed:** `has_ssl` never measured SSL. It was set from "did an
https GET return 200", which conflates a TLS failure with a 403 aimed at
scrapers and with a 404 on a dead domain:

```
scientificsearch.com    HTTP 403 (bot-blocked)  -> has_ssl=False
theascendantgroup.com   HTTP 404 (parked Wix)   -> has_ssl=False
```

Both serve valid certificates. The generator then told one of them their
site was "still running on plain HTTP" -- a false, checkable claim about
a stranger's business, which is the most damaging thing this pipeline can
emit. TLS is now answered by completing an actual handshake
(`probe_tls`), and no HTTP status influences it.

`site_status` is a new field recording what is actually at the domain:
`site_parked`, `site_unreachable`, `site_bot_blocked`, or blank for a
live site. A parked domain is not a bad website, it is the absence of
one, and `icebreaker.observations()` suppresses every site-quality signal
against it -- PageSpeed happily scored a Wix "domain isn't connected to a
site" placeholder 89/100.

Thin content alone does NOT mean parked. `careerpathwayllc.com` returns
200 with nine words because it renders client-side; a naive word-count
rule flagged it and would have discarded a live business's signals.
Corroboration is required: thin AND scriptless AND no real title.

This mattered beyond cosmetics — a wrong social URL fed the scorer and
would have fed stage 5 as a "verified observation".

### [5] ICEBREAK — `outreach/icebreaker.py`

One sentence per lead. Not a whole email.

Under SendGrid, Claude wrote the entire email per send, which made A/B
testing meaningless and put the guardrails in the position of policing
free text on every send. Now:

- **Instantly holds the template** — the constant, the thing being tested
- **Django writes `{{icebreaker}}`** — the variable, one sentence per lead

The prompt gets only measured facts and is told that thin data is normal
and a slightly generic line is correct. Output is screened before storage
by `describe_problems()`:

- cites a score never measured → rejected
- cites a year that isn't the measured copyright year → rejected
- "I listened to your podcast", "congratulations on your award",
  "we worked with a client who…" → rejected
- model commentary ("Here's a great opening line:") → rejected
- pricing claims → checked against `copy_guard`

A rejected line means the lead **waits**. It does not go out generic,
because `push_leads` refuses any lead without an icebreaker.

Why so strict: an invented detail is worse than a bland one. It is
instantly checkable, and being caught fabricating ends the conversation
permanently.

### [6] SEGMENT — `OutreachCampaign`

One niche × geography per campaign, mapped to one Instantly campaign id.

Copy that references something specific can't be written for a blended
list, and per-campaign reply rate is the only way to learn which niche
actually wants this. Four to start: TX law, GA law, TX dental, GA dental.

Manage at `/admin/outreach/outreachcampaign/`. A campaign is only
pushable when it has **both** an `instantly_campaign_id` **and**
`active=True`.

### [7] PUSH — `outreach/instantly.py`

`push_leads()` re-checks every gate rather than trusting the query that
selected the leads — it is the last thing standing between an address and
a real send, and a missing gate at exactly this point is what caused the
whole failure.

Refuses: unsendable status · suppressed · unsubscribed · already pushed ·
no icebreaker · over `INSTANTLY_MAX_PUSH_PER_DAY`.

Custom variables that reach the campaign template:
`{{icebreaker}}` `{{city}}` `{{state}}` `{{website}}` `{{business_type}}`
plus Instantly's own `{{firstName}}` / `{{companyName}}`.

### [8] SEND — Instantly

**Verified live 2026-08-22 — 18 mailboxes across 6 domains, not 12
across 3.** An earlier read of this workspace fetched only the first
page of `/accounts` and concluded all mailboxes were Automations ones.
Walking the pagination shows the website-brand domains already exist:

| Domain | Mailboxes | Active | Warmup | Capacity |
|---|---|---|---|---|
| `getaspiredautomations.com` | 3 | 3 | 100 | 90/day |
| `goaspiredautomations.com` | 3 | 3 | 100 | 90/day |
| `seeaspiredautomations.com` | 3 | 3 | 100 | 90/day |
| **`getaspiredwebsites.com`** | 3 | **0** | 0 | **0/day** |
| **`goaspiredwebsites.com`** | 3 | **0** | 0 | **0/day** |
| **`tryaspiredwebsites.com`** | 3 | **0** | 0 | **0/day** |

The nine website mailboxes are `status=2`, `setup_pending=True`,
`warmup_start=None`. They are **created but not connected** — the
provider hookup was never finished, so warmup has not begun and they
send nothing. That is what puts the earliest real send in early
September: finishing setup starts a 2–3 week warmup clock.

**The domains do not need buying. The nine mailboxes need connecting.**

Do not solve this by sending from the Automations mailboxes instead.
They are warm and available, but a website-design pitch arriving from
`getaspiredautomations.com` and linking to `aspiredwebsites.com` reads
as a bait-and-switch, and any complaints it earns burn the Automations
brand those were built for.

This module **will not activate a campaign**. `create_campaign()` builds
one paused. Starting it is a deliberate click in Instantly's UI, because
that is the irreversible step that puts mail in front of real people.

### [9] EVENTS — `outreach/instantly_poll.py` (polling, not webhooks)

**Instantly gates outbound webhooks behind a higher plan tier.** Probed
2026-08-22 on the current plan:

| Endpoint | Result |
|---|---|
| `GET /api/v2/emails` (unibox) | **200** |
| `GET /api/v2/emails/unread/count` | **200** |
| `GET /api/v2/campaigns/analytics` | **200** |
| outbound webhooks | requires plan upgrade (~$100/mo) |

So replies arrive by **polling the unibox**, not by webhook. That is not
just the cheap fallback — it is better in two ways. There is no public
endpoint to secure and no shared secret to leak (the webhook needs an
unguessable URL precisely because Instantly does not sign its payloads).
And delivery is exactly-once by construction rather than at-least-once
against a hostile internet.

The cost is latency: a reply is seen at the next poll instead of
immediately. At a 15-minute beat that is fine — nobody expects a cold
email answered in ninety seconds, and the draft waits for your approval
either way.

```
poll_instantly_replies_task()   # beat: every 15 min
```

**Bounces arrive as mail, not as events.** The webhook had a distinct
`email_bounced` event; polling does not. A bounce shows up in the unibox
as a message from `mailer-daemon@` whose body names the address that
failed. So each message is classified *before* the reply filter sees it
— otherwise a bounce would be discarded as "automated sender" and the
dead address would keep receiving mail.

The failed address is matched against leads we actually pushed rather
than taken as the first address in the notice. Otherwise a bounce would
suppress `postmaster@`, our own sending mailbox, or a support URL.

**The reply filter — the thing that did not exist.** A message is
dropped when the sender is:

1. one of our own domains (`aspired-ai.com`, `aspiredwebsites.com`,
   `*aspiredautomations.com`, `moonieful.com`, `DEFAULT_FROM_EMAIL`'s
   domain) ← *this is the exact prod bug*
2. an automated mailbox (`no-reply@`, `notifications@`, …)
3. an out-of-office / autoresponder
4. an address we never emailed

The filter runs **even when the sender matches a Lead row**, deliberately.
Prod has a Lead for "Aspired AI LLC" carrying `hello@aspired-ai.com`, so
gating the filter behind "did we find a lead" would rebuild the bug
exactly.

Both ingest paths — the poller and the webhook — converge on the same
`process_event()`. One filter, one set of handlers. A filter that applies
to only one door is not a filter.

`email_sent` advancing the sequence clock still matters: under SendGrid
it advanced at *generation* time, so a draft that never dispatched froze
the lead forever.

**The webhook endpoint stays in the codebase**, unused, for if you ever
take a plan that includes it. It 403s everything while
`INSTANTLY_WEBHOOK_SECRET` is unset, which is the correct and current
state. **You do not need to set it.**

### [10] CLASSIFY — `outreach/classifier.py`

Fires on a genuine reply. Classifies and drafts a response for approval.

The import bug (`_from_address` imported from `dispatcher` instead of
`sender`) was fixed earlier this session, so this path has **never
actually run in production**. Expect the first real reply to find
something.

### [11] BOOK — scheduler

`record_booking` is wired to `confirm_slot`. Never exercised with a real
booking; the first one will find bugs.

---

## Running it

```bash
python manage.py outreach_status                    # where does it stop?
python manage.py outreach_status --check-instantly  # + live mailbox capacity
```

Celery tasks (`outreach/tasks.py`):

| Task | Does |
|---|---|
| `verify_leads_task()` | verify pending leads — free, safe to run often |
| `generate_icebreakers_task()` | one line per sendable, enriched lead |
| `push_to_instantly_task()` | push ready leads to their campaign |
| `run_outreach_pipeline_task()` | all three in order — one beat entry |

Sourcing is deliberately **not** in the pipeline task: it costs money per
run and is driven by `ScrapeJob` on its own schedule, so a pipeline retry
can never re-trigger a paid scrape.

Order is not arbitrary — verification runs before icebreaker generation
because an icebreaker costs a Claude call and a role address is worth
zero of them.

---

## What is not done

- [ ] **Verification vendor not chosen.** Until `EMAIL_VERIFY_PROVIDER`
      is set, everything sits at `unverified` and nothing can be pushed.
      This is the current hard blocker.
- [ ] **The 9 website-brand mailboxes are not connected.**
      `setup_pending=True`, warmup never started. Finishing setup is what
      starts the 2–3 week clock to the first real send. This is the
      longest-lead-time item — do it first, it blocks nothing else.
- [ ] **No campaigns exist** in Instantly or in Django.
- [x] ~~Webhook secret~~ — **not needed.** Webhooks require a plan
      upgrade; the poller replaces them and needs no secret.
- [ ] **Sequence copy not written.** `create_campaign()` takes the steps;
      nobody has written the 4 touches.
- [ ] **Apify free-plan API refusal** — UI + dataset import, or upgrade.
- [ ] Prospect's agent runtime still does not exist; none of this is
      autonomous yet. It is a pipeline you run, not an employee.

## Deploy notes

- New migration `outreach/0015` — additive columns plus two new tables.
  Nullable/defaulted throughout, so no table rewrite and no lock concern.
- New env vars, all defaulted to safe-off: `INSTANTLY_TOKEN`,
  `INSTANTLY_WEBHOOK_SECRET`, `INSTANTLY_MAX_PUSH_PER_DAY`,
  `EMAIL_VERIFY_*`. `.env` is gitignored — set them on the server.
- Nothing in this build sends an email or activates anything. It is safe
  to deploy before the decisions above are made.
