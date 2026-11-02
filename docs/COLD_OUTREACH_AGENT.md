# Prospect — Cold Outreach Agent

**Status:** Phase 1 built (2026-08-19). Phases 2+ blocked on Apify/Instantly account details.
**For:** Claude Code, working directly in this repo.

This supersedes the original `COLD_OUTREACH_AGENT.md` brief and its
`COLD_OUTREACH_AGENT_CORRECTIONS.md` addendum — both are folded in here.
Where they disagreed, this file wins.

The agent is named **Prospect**. It is registered in the AI Employees
registry (`admin_dashboard.AIEmployee`, slug `prospect`), seeded **paused**.

---

## 0. What this replaces

The outreach system was a fixed pipeline: scrape → enrich → score →
generate one email via one Claude call → approve → send via SendGrid →
poll IMAP for replies. The scraping and sending layers are being replaced
rather than hardened further, and an actual agent is being added on top.

**Kept as-is:** `Lead`, `LeadNote`, `EmailReply`, `SuppressionList`. The
admin dashboard as the cockpit. `outreach/scoring.py`'s job.
`outreach/copy_guard.py`'s job (extended, not replaced).
`outreach/gating.py`'s trust-level concept.

**Replaced:**
- `outreach/scraper.py` (Places API + two never-tuned State Bar Playwright
  scrapers) → **Apify actors** (§3).
- `outreach/dispatcher.py` + `outreach/warming.py` → **Instantly** (§4).
- `outreach/reply_ingest.py` (IMAP polling) → **Instantly webhook** (§4).
- `sender.py`'s single-shot generate call → the agent loop (§5).

**Unchanged constraint:** the `EmailSent.status` lifecycle
(`pending_approval → approved → sent`) and
`outreach.gating.should_queue_for_approval` stay the mechanism for "does
this need a human". Point them at Instantly instead of SendGrid; do not
reinvent the approval gate.

---

## ✅ Step 0 — the two live bugs (DONE 2026-08-19)

Both were live in this repo; the original brief wrongly claimed they were
already fixed (the fixes had been made in a sandboxed clone that was never
pushed).

1. **DONE** — `outreach/classifier.py` imported `_from_address` from
   `dispatcher`; it lives in `sender.py`. Every non-unsubscribe reply hit
   an `ImportError` and was never auto-drafted.
2. **DONE** — `outreach/sender.py` advanced `lead.sequence_step` and
   `lead.next_followup_at` at *generation* time. Both now move only in
   `dispatcher.dispatch_approved_batch` on a confirmed send, guarded on
   `kind == 'cold'` and monotonic. `_eligible_leads` additionally excludes
   leads holding `pending_approval`/`approved` mail, or a backlog of
   unapproved drafts would starve never-contacted leads.

Do **not** build `outreach/unsubscribe.py`, its view, or its URL — they do
not exist in this repo and Instantly owns opt-out from §4 onward.

Shipped alongside step 0:

3. **DONE** — `outreach` added to `LOGGING['loggers']` in settings.py. The
   cold sender, dispatcher, reply ingest and agent loop all log their
   decisions; without the logger those lines fell through to root and were
   invisible in supervisor output.
4. **DONE** — `enricher.py` now `html.unescape()`s markup before extraction
   (sites emit `info&#64;firm&#46;com` and `&#169; 2019`, which the regexes
   missed entirely) and requires `EMAIL_RE.fullmatch()` on the winning
   candidate, stripping `?subject=` tails and trailing punctuation. A
   malformed address becomes a hard bounce and a suppression entry.
5. **DONE** — `dispatcher.py` now distinguishes permanent from transient
   send failures. Its docstring always claimed permanent failures should
   stop rather than hammer SendGrid; the code retried *everything* on
   `approved` every 30 minutes forever. Permanent now → `rejected` +
   address suppressed. Unrecognised errors default to transient: a wrongly
   transient call costs one retry, a wrongly permanent one silently drops a
   real prospect.
6. **DONE** — data-health check for a frozen sequence clock (see §4 step 6).

---

## ✅ 1. Guardrails — the three things Prospect cannot cross (DONE)

Hard blocks, not prompt instructions.

1. **Pricing (DONE).** `copy_guard.describe_pricing_problems(body, subject)`
   flags any dollar figure, percentage-off, or discount language and
   rejects it unless it matches an active `ServiceTier.price_display`
   exactly. Wired into **both** `sender._split_subject_body` (so a made-up
   price never reaches an `EmailSent` row) and `dispatcher` (last gate
   before SMTP). **Fails closed:** a DB error yields an empty allow-list,
   so every price is rejected rather than waved through.
2. **Templates (DONE for the data model).** Prospect may choose among
   `active=True` `EmailTemplateVariant` rows. It may not freehand a new
   angle into rotation — `propose_new_template_variant` (§5) creates the
   row `active=False` plus an `AIEmployeeAction` awaiting approval.
   `EmailTemplateVariant.active` defaults to `False` so this holds by
   construction.
3. **Spend — TWO SEPARATE CAPS, never one pool.** Claude bills per token
   and accumulates smoothly; Apify bills per run/compute-unit and one bad
   call can burn a chunk of budget in a single request. Sharing a pool
   means a runaway scrape silently eats the reasoning budget and Prospect
   goes quiet for the day with no obvious cause.

   **Cap A — Claude/LLM (DONE).** `OutreachSettings.daily_ai_spend_cap_usd`,
   default **$10.00/day**. `spend.check_spend_allowed()`. Ledger is
   **today's `AIEmployeeRun.spend_usd` summed**, written incrementally as
   a run proceeds — *not* `ClaudeUsage`, which is a per-month per-model
   rollup (`unique_together = ['year_month','model']`) and cannot answer
   "how much today". `ClaudeUsage` keeps its own job.

   **Cap B — Apify (config DONE, ledger with §3).** Bounded by run count
   and result count rather than dollars — the agent knows how many runs
   it has left far more reliably than it can predict compute cost, and a
   run ceiling is what actually stops a runaway scrape.
   `OutreachSettings.apify_max_runs_per_day` (default **3**) and
   `apify_max_results_per_run` (default **100**), gated by
   `spend.check_apify_allowed()`. Both gates return a "quota reached"
   *string* the agent can act on, never an exception — it must wrap up
   cleanly, not read a failure as something to retry.

---

## ✅ 2. Data model (DONE)

`outreach.EmailTemplateVariant` — name, `sequence_step`,
`angle_instructions`, `active` (default False), `proposed_by`, plus
denormalised `sends/opens/replies/bookings` counters (read on every agent
run; must not become a join across `EmailSent`).

`EmailSent.template_variant` FK (null for replies and pre-variant rows)
powers the stats rollup and the rotation maths.

**Seeded:** `outreach/migrations/0011_seed_baseline_template_variants.py`
lifts the four `step_brief` entries from `sender._user_prompt_for_step`
verbatim as active "Baseline" variants, one per step.

**System prompt vs angle — settled.** The shared system prompt (brand
voice, tone, Aspired constraints) stays in `sender._system_prompt()` in
code, identical for every variant. `angle_instructions` holds only the
per-variant angle appended to it. The thing under test is the *angle*, not
how Aspired sounds.

---

## 3. Apify — lead sourcing (BLOCKED — see §10)

New module `outreach/apify_source.py` with
`run_lead_search(query, location, max_results, business_type)`, returning
dicts shaped exactly like `scraper.py`'s old output so
`pipeline.import_leads` and `enricher.py` are untouched — Apify replaces
*finding* leads, not enriching or scoring them.

`ScrapeJob.source` gains an `'apify'` choice; `tasks.run_scrape_jobs_task`
gains a matching branch. Use the `apify-client` package, not raw HTTP.

**Apify quota — build this in, do not bolt it on.** The config already
exists (`apify_max_runs_per_day`, `apify_max_results_per_run`) and
`spend.check_apify_allowed()` already gates on it. §3 must supply the
missing ledger and honour the rest:

1. Add an `ApifyRun` model — `started_at`, actor id, query, `results_requested`,
   `results_returned`, `estimated_cost_usd`, status. `spend.apify_runs_today()`
   already looks for it and degrades to 0 until it exists.
2. **Record the estimated cost BEFORE the run starts**, not after. A run
   that dies mid-flight still consumed compute; costing it only on success
   under-reports exactly when it matters most.
3. Clamp every request to `apify_max_results_per_run` — never pass the
   agent's requested `max_results` through unchecked.
4. Call `check_apify_allowed()` first and return its reason string to the
   agent verbatim on refusal. Hard stop, not an exception.
5. Apify spend must **not** be written to `AIEmployeeRun.spend_usd` — that
   field is the Claude ledger. Keep the pools separate.

Delete `scrape_texas_bar_sync` / `scrape_georgia_bar_sync` and the
`playwright` pin once Apify is confirmed working — check nothing else
needs Playwright first.

---

## 4. Instantly — sending, warmup, replies (BLOCKED — see §10)

New module `outreach/instantly.py`. Numbered, in order:

1. **Configure the physical postal address in Instantly's own campaign /
   account settings BEFORE the first send.** This does *not* carry over
   from `core/email_backend.py` — that is a Django SMTP backend subclass
   and only stamps mail passing through Django's send path. Once Instantly
   sends directly via its API, that footer never applies. This is the same
   CAN-SPAM requirement the abandoned `unsubscribe.py` existed to satisfy;
   do not let it silently drop because the sending path moved.
2. `add_lead_to_campaign(lead, campaign_id, personalization)` — POST to
   `/v2/leads`, handing Instantly the drafted subject/body as
   personalization variables.
3. `get_campaign_analytics(campaign_id)` — reply/open rates, feeding the
   daily digest (§7) and the variant stats rollup (§2/§6).
4. Register the **reply-received** webhook → new endpoint
   `outreach/views.py::instantly_webhook`. On receipt: write an
   `EmailReply` row exactly as `reply_ingest.py` did, then fire
   `classify_and_draft_reply_task`. `classifier.py` does not change — only
   what triggers it does. Verify the payload signature the way
   `sendgrid_webhook.py` does; do not ship an unauthenticated public
   endpoint that can write `EmailReply` rows.
5. Replace the `send_approved_emails_task` beat entry with a task that
   pushes `approved`-status `EmailSent` rows to Instantly via
   `add_lead_to_campaign`.
6. **Register the Instantly "email sent" webhook and re-anchor the
   sequence-step advance to it.** Step 0 put the
   `sequence_step` / `next_followup_at` advance in
   `dispatcher.dispatch_approved_batch`'s synchronous success path. Once
   dispatcher.py is deleted there is **no synchronous send moment** —
   handing a lead to a campaign is not the same as Instantly sending it.
   Without this step the sequence clock silently stops advancing and every
   lead freezes at step 1. Move the advance (and `record_send`) into this
   webhook handler. Do **not** let it fall back to generation time; that is
   the bug step 0 just fixed.
7. Delete `outreach/dispatcher.py` and `outreach/warming.py` only after
   steps 1-6 are confirmed working.

**Known limitations:** Instantly DFY mailboxes currently provision Google
accounts only, and DFY domains cannot be transferred out of Instantly.

---

## 5. The agent

### ✅ 5.1 The loop primitive (DONE)

`reporting.ai.claude_agent_loop(system, tools, tool_executor,
user_message, model, max_steps, max_tokens, effort, on_usage)` — a full
multi-turn tool-use loop. Returns
`{'transcript', 'final_text', 'stopped_reason', 'steps_used'}`.

- `tool_executor` must not raise past its own boundary; the loop catches
  anyway and returns the error as a tool *result* so Claude can adapt.
- `on_usage(model, in_tokens, out_tokens)` fires after **every** API call —
  this is how the spend cap stays honest across a crashed run.
- `max_tokens` defaults to **8,000**, not the 2,048 originally specced:
  Sonnet 5 shares that budget between adaptive thinking, tool_use blocks
  and visible text. Above 16,000 the loop streams and uses
  `.get_final_message()` to dodge the SDK's HTTP timeout.
- Records `ClaudeUsage` per call, so an 8-call run is 8 recordings.
- Returns `messages` — the real conversation in Anthropic wire shape,
  serialised to plain dicts as it goes (see `_serialise_content_blocks`).

**Conversational chat — future-proofed, not built.** `AIEmployeeRun`
carries a `message_history` JSONField holding that list verbatim: real
`{role, content}` dicts with `text` / `tool_use` / `tool_result` blocks,
thinking-block signatures intact. Nothing reads it yet.

The reason it exists now: a chat pane means passing prior turns back into
the loop, which needs genuine message objects. Rebuilding those from the
`summary` string afterwards is lossy and would cost a migration plus a
backfill. Storing the right shape today costs a JSONField.

`summary` is unchanged and stays the human-readable journal entry that
the run log renders and that feeds the next run's system prompt — the two
are additive, not alternatives.

**Do not** build the chat UI or cross-run thread persistence yet. Just
don't throw away the data that would make them cheap.

**Model (DONE):** `MODEL_CONTENT = 'claude-sonnet-5'`. Sonnet, not Opus —
a deliberate cost call. Sonnet 5 was not a bare ID swap; see §5.5.

### 5.2 Tools Prospect gets (PENDING — needs §3/§4)

New module `outreach/agent.py`:

- Read-only: `get_pending_work`, `research_lead`, `get_template_variants`.
- Gated: `propose_email` (runs `describe_copy_problems` +
  `describe_pricing_problems` **before** writing the row — reject, don't
  queue), `propose_new_template_variant` (writes `active=False` + an
  approval-required `AIEmployeeAction`), `queue_apify_search` (checks
  `spend.check_spend_allowed()` first), `request_human_decision`,
  `log_note`.

Every mutating tool call also writes an `AIEmployeeAction` so the run log
is a complete inspectable trail, not a summary string.

### 5.3 Memory (PENDING)

Each run's system prompt carries: today's date, current guardrail state
(trust level, spend used today, active variants), and
`AIEmployee.last_journal_entry` — the short summary Prospect itself wrote
via `log_note` at the end of its previous run. That is what makes it adapt
across runs instead of re-deriving from zero.

### 5.4 Scheduling (PENDING)

One agent, three trigger paths: `scheduled` (Celery beat, hourly per
`AIEmployee.run_interval_minutes`), `reply_webhook` (immediate,
narrowly-scoped run from §4 step 4), `manual` ("Wake now" button).

### ✅ 5.5 Sonnet 5 migration notes (DONE)

- **Adaptive thinking is ON when `thinking` is omitted** (4.6 defaulted
  off) and `max_tokens` caps thinking + visible output *together*.
- **`response.content[0]` is no longer the text block** — it is a
  `ThinkingBlock`. `reporting.ai._first_text_block` now walks the blocks;
  index-0 access raised `AttributeError` on every `MODEL_CONTENT` call.
- Generation calls raised for headroom: `sender` 600→3000, `classifier`
  `_draft_reply` 500→3000, `social/ai` 900→3000, `views_case_studies`
  1200→4000.
- Structured-JSON calls pass `thinking: {'type': 'disabled'}` — the three
  raw-HTTP calls in `clients/intelligence.py`. `classifier._classify`
  needs nothing: it runs on `MODEL_CHAT` (Haiku 4.5), which is unaffected.
- Manual `thinking: {'type':'enabled', 'budget_tokens': N}` now 400s;
  non-default `temperature`/`top_p`/`top_k` now 400. Neither appears here.
- New tokenizer, ~30% more tokens for the same text.
- **`CLAUDE_PRICING_USD_PER_MTOK` must carry every model this codebase can
  emit** — `cost_usd` returns 0.0 on a miss, which would zero the spend
  cap. `claude-sonnet-4-6` is retained because `vault/views.py`'s
  `OPS_AGENT_MODEL` hardcodes it and does not follow `MODEL_CONTENT`.
  A test enforces this.

---

## ✅ 6. Adaptive variant rotation (DONE, weighting dormant)

`outreach/variant_rotation.py::choose_variant(step)` → `(variant, reason)`.

This business will not reach statistical significance at
`daily_send_cap = 15` across four steps. So:

- **Live default:** the most-established variant (highest `sends`).
  Splitting thin volume across variants learns nothing about either.
- **Dormant:** weighted rotation by reply rate, with `MIN_SAMPLE_SIZE = 35`
  and a `FLOOR_ALLOCATION = 0.15` floor so a newer variant is never
  starved and an early lucky streak cannot lock rotation in. Written and
  tested; switch on via `WEIGHTED_ROTATION_ENABLED` when volume justifies.
  **Do not delete the maths in the meantime** — it gets switched on, not
  rewritten.

Counters move on real events: `record_send` in dispatcher (on confirmed
send, not draft), `record_open` in the SendGrid webhook, `record_reply` in
reply ingest.

*(Module named `variant_rotation`, not the brief's `templates` —
`outreach/templates.py` beside Django's `templates/` directory convention
is a confusion this app doesn't need.)*

---

## 7. Daily digest (PENDING)

`outreach.tasks.send_daily_agent_digest_task`, once daily early morning.
Per `AIEmployee`: leads found, drafted, sent, opened, replied, booked,
rejected-by-guardrail counts, and total spend (Claude + Apify) for the
prior 24h. Plain text via the existing email backend.

---

## 8. AI Employees dashboard

### ✅ 8.1 Models (DONE)

In `admin_dashboard/models.py` — **not** `outreach/`. The registry is
designed to hold agents unrelated to outreach (Research Agent, SEO Audit
Agent), and the cockpit views + nav badge already live in that app.

- `AIEmployee` — name, slug, role_description, active,
  `run_interval_minutes`, **`reasoning_effort`** (low/medium/high/xhigh/max,
  default **medium** — deliberately below the API's own `high`, same
  conservative posture as `trust_level` starting at 1), and
  `last_journal_entry`.
- `AIEmployeeRun` — trigger, timings, status, summary, steps_used,
  `spend_usd` (the §1.3 ledger).
- `AIEmployeeAction` — one row per tool call; `requires_approval` /
  `approved` drive the badge.
- `AIEmployeeTask` — manually-assigned instruction picked up next run.

Plain integer PKs, not `TimestampedModel`: UUIDs exist so Aspired and
Moonieful IDs never collide across the sync bridge, and none of this data
crosses it.

**Seeded:** `admin_dashboard/migrations/0006_seed_prospect_ai_employee.py`
registers **Prospect**, `active=False` (paused). Nothing that can email a
real prospect starts itself.

### 8.2 Views + URLs (PENDING)

`admin_dashboard:ai_employees` (card per employee) and
`admin_dashboard:ai_employee_detail` (by slug — run log, expandable to
`AIEmployeeAction` steps, "Wake now", free-text box creating an
`AIEmployeeTask`, and this employee's pending approvals).

### 8.3 Badge (PENDING — settled)

AI Employees badge counts **`AIEmployeeAction` pending only**.
`EmailSent.pending_approval` stays with the existing Approvals nav item —
no double-counting the same queue under two badges. Use the same
defensive `try/except` pattern as every other count in
`admin_dashboard/context.py`.

---

## 9. Build order

1. ✅ Step 0 bugs, §1 guardrails, §2 data model, §5.1 loop, §5.5 model
   migration, §6 rotation, §8.1 models.
2. ⛔ §3 Apify and §4 Instantly — parallel, both blocked on §10.
3. §5.2–5.4 agent runtime — needs 1 and 2.
4. §6 weighting — switch on when volume justifies.
5. §8.2/8.3 dashboard — needs §5 to have produced a run worth looking at.
6. §7 digest — last, cheap.

---

## 10. Blocked on account details — do not guess

- Which Apify actor for lead search, and its exact input schema. Pick one
  in Apify Console and confirm; do not assume `compass/crawler-google-places`
  is still right.
- Instantly campaign structure: one campaign per sequence, or one campaign
  with steps as stages? This determines how `add_lead_to_campaign` maps to
  `EmailSent.sequence_step`.
- Are both accounts provisioned with API keys ready for `.env`?

Block and ask. Do not build against a guessed actor ID or campaign shape.
