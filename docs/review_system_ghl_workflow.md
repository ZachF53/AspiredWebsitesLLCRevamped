# Review System — the GoHighLevel side

The Django app collects setup, holds the approved wording, and reports.
**GHL sends the messages.** Everything in this file is manual work done
once per client, in the GHL UI. Nothing in the codebase can enforce it,
which is exactly why it is written down.

Per-client setup order:

1. Create a GHL **sub-account (location)** for the client.
2. Submit their **A2P 10DLC brand** using the details on
   `/admin-dashboard/reviews/<program_id>/` — legal business name, EIN,
   address, business phone, website. These must match the client's IRS
   records exactly; a mismatch is rejection cause #1 and costs a 2–5 day
   round trip. For a **sole proprietor** (no EIN) register a sole-prop
   brand instead — lower throughput (~1,000 msgs/day), no short-code
   reach, which is fine at one-truck volume.
3. Submit the **campaign**. It needs sample messages, an opt-in
   description, STOP instructions, and **HELP instructions with a
   working reply**. Carriers filter campaigns that lack the last one.
4. Buy the **phone number** in that sub-account.
5. Paste the **location ID**, the **Private Integration Token** and the
   **sending number** into the admin Connection panel, and set the A2P
   statuses as they progress. The client sees a friendly waiting page
   until both are approved.
6. Build the workflow below.
7. Press **Mark live**. It refuses unless the location ID, sending
   number, both A2P approvals, the Google review link, and a **consent
   attestation** are all on file. The attestation can only be created by
   the client completing the onboarding wizard themselves.

Request the throughput their `avg_jobs_per_week` answer implies — a
100-job/week client on a default-trust campaign will hit the carrier
daily cap and silently lose messages.

---

## The workflow

**Trigger:** the client's job system marks a job complete. Either a
native webhook into GHL or a Zap. The app is deliberately not in this
path — job events go straight to GHL.

### Step 1 — ask us whether you may send. REQUIRED.

An HTTP GET to:

```
https://aspiredwebsites.com/api/reviews/status/<public_key>/
```

The `public_key` is on the admin program page. It returns:

```json
{"paused": false, "live": true, "reminder": true}
```

**Exit the workflow when `paused` is true.** Without this step the
client's Pause button in the portal does nothing — it flips a flag
nobody reads, and we would be telling them sending had stopped while it
continued. It is also the kill switch if something goes wrong.

Branch on `live` too, so a workflow built before the carriers approve
the number does not start sending early.

### Step 2 — send the SMS and the email

Use the **approved wording** from the admin page, verbatim. When the
client edits it in the portal the program is flagged *"Update GHL"* in
the queue; copy the new text across and press **Mark GHL workflow
updated**.

Two things are locked and must survive any edit:

- `{review_link}` — the client's Google review URL.
- `Reply STOP to opt out, HELP for help.`

The app rejects wording that offers an incentive or filters by expected
rating, because both breach Google's review policy. **Do not reintroduce
either by typing it straight into GHL** — that is the one path the
guardrails cannot see.

### Step 3 — the reminder

Wait 3 days. Send once more only if the link was never clicked and the
contact has not opted out. Honour the `reminder` field from step 1 so
the client's portal toggle works.

### Step 4 — report back

POST to:

```
https://aspiredwebsites.com/api/reviews/ghl/events/<REVIEW_GHL_EVENTS_SECRET>/
```

The secret is in `.env` as `REVIEW_GHL_EVENTS_SECRET`. **The endpoint
403s everything while that setting is unset**, so set it before building
the first workflow.

```json
{
  "event_type": "sent",
  "locationId": "<the sub-account location id>",
  "messageId": "<anything stable and unique>",
  "customer": {"name": "Dana Ruiz", "phone": "+12105550134",
               "email": "dana@example.com"},
  "timestamp": "2026-10-14T18:22:00-05:00"
}
```

`event_type` is one of `sent`, `delivered`, `reply`, `click`,
`review_left`, `opt_out`, `failed`. Add a step for each one the workflow
can observe — the portal dashboard shows only what GHL reports, so an
event you never send is a number the client never sees.

`locationId` is what attributes the event to a client. If it is missing
or wrong the event is still stored, but it lands in the "unknown
location" count on the admin queue instead of the client's dashboard.

`messageId` makes retries safe: a duplicate is answered `200
{"status":"duplicate"}` and stored once. Without one we fall back to
hashing the payload, which is weaker.

Include `rating` on a `review_left` event if GHL has it. That is
read-only reporting of a review that is already public — it never
decides who gets asked.

### Step 5 — negative replies

On a reply that reads negative: fire the internal SMS to the operator
and the notification email to the client, as agreed. Also POST it as a
`reply` event so it appears in the client's portal and in the admin
queue — the app flags any reply that is not a carrier keyword as needing
a human.

---

## What must NOT be in the workflow

**No "how did we do?" pre-screen.** Not a star picker, not a 1–5
rating step that routes high scores to Google and low scores to an
internal form. That is review gating. It is against Google's policy, it
is the single most common way agencies get a client's listing
penalised, and the public service page promises we do not do it.

Every customer gets the same message with the same link.
