# Moonieful ↔ Aspired Websites Sync Contract (v1)

This is the one written reference for the wire format both sides implement against.
It exists because the two `sync` apps live in separate repos, were built months apart,
and drifted apart from each other with nothing to catch it. **If you change this
contract, update the copy in both repos and the code on both sides in the same change.**

Moonieful repo: `sync/bundle.py`, `sync/security.py`, `sync/transport.py`,
`sync/handlers.py`, `sync/views.py`.
Aspired repo: `sync/security.py`, `sync/handlers.py`, `sync/views.py`,
`sync/signals.py`, `sync/transport.py`, `sync/management/commands/run_sync.py`.

## HMAC envelope (both directions)

```
canonical_message(timestamp, raw_body) = f'v1\n{timestamp}\n'.encode('utf-8') + raw_body
signature = HMAC-SHA256(shared_secret, canonical_message).hexdigest()
```

Headers on every signed request:

| Header | Value |
|---|---|
| `X-Sync-Version` | `v1` |
| `X-Sync-Timestamp` | unix timestamp (seconds), signed fresh per attempt |
| `X-Sync-Signature` | hex HMAC-SHA256 over `canonical_message(timestamp, raw_body)` |

The signature **must** cover the timestamp, not just the body — otherwise a captured
`(body, signature)` pair can be replayed forever by attaching a fresh timestamp.
Verification must fail if either the signature or the ±300s freshness window fails.
A shared secret under 32 characters is treated as not configured — refuse to sign or
verify rather than using a weak/placeholder key.

**File transfer** uses the same scheme but signs `f'{url}\n{size}'` (not the file
bytes — hashing a large upload twice is wasteful and the file endpoint's authority
comes from the same shared secret either way). The body of the request is the raw file
bytes (`Content-Type: application/octet-stream`), **not multipart form data**. The
filename travels in `X-Sync-Filename` and must be base-name-only on receipt (strip any
path component before saving, to block path traversal).

## Direction 1 — Moonieful → Aspired (client bundle)

`POST /api/sync/inbound/` — every event type sends the **full current bundle**, never a
partial diff:

```json
{
  "schema_version": 1,
  "source_site": "moonieful",
  "event_type": "client_created | client_updated | stage_changed | document_added | project_complete",
  "event_id": "uuid",
  "synced_at": "iso8601",
  "client": {
    "moonieful_client_id": "uuid",
    "created_at": "iso8601",
    "updated_at": "iso8601",
    "account": {
      "email": "...",
      "username": "...",
      "first_name": "...",
      "last_name": "...",
      "is_active": true,
      "password_hash": "pbkdf2_sha256$..."
    },
    "business_name": "...",
    "phone": "...",
    "website": "...",
    "moonieful_package": "...",
    "status": "..."
  },
  "stage_history": [
    {"id": "uuid", "stage_name": "...", "note": "...", "created_at": "iso8601", "updated_at": "iso8601"}
  ],
  "documents": [
    {"id": "uuid", "label": "...", "description": "...", "direction": "to_client|from_client",
     "filename": "...", "file_ref": "uuid", "created_at": "iso8601", "updated_at": "iso8601"}
  ],
  "intake": [
    {"form_title": "...", "submitted_at": "iso8601",
     "answers": [{"question_text": "...", "question_type": "...", "value_text": "...", "file_ref": "uuid|null"}]}
  ],
  "revision_requests": []
}
```

`revision_requests` is always empty — revisions never originate on the Moonieful side
(locked decision). `POST /api/sync/file/<file_ref>/` streams the file body for any
`file_ref` referenced above, sent after the JSON bundle succeeds.

Response on success: `{"status": "ok", "aspired_client_id": "uuid"}`.
Duplicate `event_id` (already applied): `{"status": "ok", "detail": "already applied", "duplicate": true}`.

**Note for the receiver**: a Moonieful client maps to exactly one Aspired **Account**,
but an Account can own multiple **Websites**. Resolve "the website for this sync" by
filtering for the one already flagged as the Moonieful referral — never assume "the
account's oldest/only website," since the same person may separately be a direct
Aspired client with an unrelated build.

### What Aspired does with it

- **Documents.** Every field is kept: `label`, `description`, `direction` (a client's own
  upload stays `from_client`), `filename`, and the optional per-document keys below.
  Documents are keyed by `file_ref` (falling back to `id`), which is the id the file
  endpoint is called with. Metadata is last-writer-wins on the document's `updated_at`:
  an older bundle never rolls a newer label or description back. `client_updated`
  upserts `documents` too, not only `client_created` / `document_added`.
- **Intake.** Stored verbatim and shown on Aspired's v2 Intake tab as question/answer
  pairs, with file answers linked to their downloads. Intake file answers are stored as
  `from_client` documents with category `intake`. A synced website is marked intake-complete
  on arrival. Moonieful owns intake for these clients, so they are never sent to Aspired's
  own intake form.
- **Stage history and package.** Refreshed on every `client_updated` and
  `stage_changed` event, and shown read-only.
- **Audit log.** `client.account.password_hash` is replaced with `"[redacted]"` before the
  bundle is written to `SyncLog`. The handler runs in a transaction, and the log row records
  the real outcome (`processed` / `failed` / `skipped`).

### File endpoint limits

`POST /api/sync/file/<file_ref>/` accepts up to **500 MB** (`SYNC_MAX_FILE_SIZE`). This
matches Moonieful's own upload limit. The body is streamed to disk, never held in memory.
Requirements:

- `Content-Length` is required (411 without it). The signature is checked against it
  before any byte is read. A body that doesn't match it is rejected (400).
- A file over the limit returns 413.
- An extension outside the allow-list returns 400. The list is every type Moonieful's
  form allows except archives (`.zip` etc.) and `.html`/`.htm`.
- A re-send replaces the stored file.
- Every attempt, success or failure, is logged as a `file_received` `SyncLog` row, visible
  on the client's v2 Moonieful tab.

Received files are stored privately, outside the public media directory. They are only
served through auth-checked, forced-download views, with a `sandbox` CSP and `nosniff`.

Nginx in front of Aspired needs a matching `location /api/sync/file/` block with
`client_max_body_size 500M; proxy_request_buffering off; proxy_read_timeout 300s;` (see
`admin_dashboard/templates/admin_dashboard/_deploy_steps.html`, step 8). Gunicorn's
worker `--timeout` must also cover the slowest expected upload.

### Optional extension keys (additive — `schema_version` stays 1)

Aspired's v2 dashboard mirrors everything Miki sees for a client. Aspired already accepts,
stores and displays the keys below. Moonieful can start sending any of them at any time,
in any event's bundle.

**Key semantics:**
- A key that is present **replaces** what Aspired holds for it.
- A key that is absent is left alone.
- An empty list means "none".
- Values are rendered as text only, never as HTML.
- Records are shown in the order sent.
- `id` is hidden, and every other field becomes a column.

The fields in brackets are what Aspired would like, taken from her models.

**Per-document keys** (inside `documents[]`):
- `category`: blueprint / contract / invoice / asset / deliverable / meeting / other
- `visible_to_client`: bool. Hidden files stay visible in Aspired admin but are removed
  from the client's Aspired portal.
- `is_deleted` (bool) or `deleted_at` (iso8601 | null): shown flagged in admin, removed
  from the portal. **Please include soft-deleted documents with this flag** rather than
  dropping them, so Aspired can tell a deletion from "never sent".

**Top-level keys:**

| Key | Shape | Fields (from Moonieful's models) |
|---|---|---|
| `stage_details` | list | `id, name, status, is_current, order, started_at, completed_at, meeting_notes, client_action_items, moonieful_action_items, recommendations` (ProjectStage + ProjectNotes) |
| `tasks` | list | `id, title, description, owner, status, due_date, notes, link, completed_at, visible_to_client` (ClientTask). The file goes in `extra_files`. |
| `meetings` | list | `id, kind, title, status, scheduled_at, notes, meeting_notes, client_action_items, moonieful_action_items, recommendations` (Meeting) |
| `contracts` | list | `id, label, status, sent_at, signed_at, notes` (Contract). The document goes in `extra_files`. |
| `invoices` | list | `id, label, amount_cents, status, due_date, paid_at, payment_url, receipt_url, notes` (Invoice) |
| `approvals` | list | `id, title, description, status, due_date, decided_at, decision_comment` (ApprovalRequest) |
| `change_requests` | list | `id, title, description, status, response, created_at` (ChangeRequest). **Display only** on Aspired: these never become Aspired revision requests, because revisions come through the Aspired portal only. |
| `recommendations` | list | `id, page, notes, status, sent_at, viewed_at` (WebPage + PageRecommendation). The screenshot goes in `extra_files`. |
| `completion` | object | `training_delivered, client_signoff_at, support_summary, support_contact, notes, completed_at` (ProjectCompletion) |
| `testimonial` | object | `body, rating, consent, display_name, business_name, website, video_url` (Testimonial). The photo goes in `extra_files`. |
| `activity` | list | `created_at, kind, summary` (ActivityEvent, newest first, capped at ~100) |

**`extra_files`** (top level) covers files attached to the records above. Each item has
the same shape as a `documents[]` item, plus a required `ref`:

```json
{"ref": "uuid", "label": "...", "category": "task|screenshot|testimonial|contract",
 "direction": "to_client|from_client", "filename": "...", "updated_at": "iso8601"}
```

Stream each one to `POST /api/sync/file/<ref>/` exactly like a document, after the bundle
succeeds. `ref` must be unique across documents, intake answers and extra files; the
record's own UUID is fine.

## Moonieful-side TODO (as of 2026-09-25)

Aspired's receiver is ready for all of the following. The work is in the Moonieful repo:

1. **Turn sync on.** Set `SYNC_ENABLED = True` and a `MOONIEFUL_SYNC_SECRET` of at least
   32 characters matching Aspired's. Install the `run_sync` every-minute cron.
2. **Lost handoffs.** The "Hand off to Aspired" button currently sets `handed_off_at` and
   says "It will sync once sync is turned on", but nothing re-queues it later. Any
   handoff clicked while sync was off must be re-sent (`project_complete`) once it is on.
3. **Build the Direction 2 `document_added` receiver** (see below). Until then, every
   Aspired→Moonieful document job fails on the file step and alerts an admin.
4. **Send the optional extension keys** above, plus `extra_files`, so Aspired sees tasks,
   meetings, contracts, invoices, approvals, change requests, recommendations, wrap-up,
   testimonial, activity and stage notes.
5. **Deletes.** Include soft-deleted documents with `is_deleted` / `deleted_at` instead of
   omitting them. No delete event is needed.
6. **Password hash.** Aspired only uses `password_hash` on `client_created`, and only
   when creating a new login. Consider sending it only on that event.
7. **Don't re-send superseded versions** as separate documents, or mark them
   `visible_to_client: false`, so Aspired's client portal doesn't list stale versions.

## Direction 2 — Aspired → Moonieful

`POST https://moonieful.com/portal/api/sync/inbound/`:

```json
{
  "schema_version": 1,
  "source_site": "aspired",
  "event_type": "stage_changed | maintenance_activated | document_added",
  "event_id": "uuid",
  "moonieful_client_id": "uuid",
  "data": {
    "aspired_project_stage": "..."
  }
}
```

or, for `maintenance_activated`:

```json
{"...": "...", "data": {"maintenance_active": true}}
```

Aspired never sends account/intake data back — per the field-ownership rule, Moonieful
owns identity/intake, Aspired owns build stage/maintenance/support. `moonieful_client_id`
is required at the top level; a payload without it is rejected.

### `document_added` (Aspired → Moonieful)

Sent when a `ClientDocument` is created locally on a Website already flagged
`moonieful_referred=True` — an admin upload from Aspired's v2 Files tab, or the client's
own portal upload. Never sent for a document that itself originated from Moonieful via
Direction 1 (loop prevention: skipped whenever `moonieful_document_id` is already set on
that row).

```json
{
  "schema_version": 1,
  "source_site": "aspired",
  "event_type": "document_added",
  "event_id": "uuid",
  "moonieful_client_id": "uuid",
  "data": {
    "document_id": "uuid",
    "filename": "brand-assets-final.psd",
    "label": "Final brand assets",
    "description": "",
    "direction": "to_client | from_client"
  }
}
```

`data.document_id` is the Aspired `ClientDocument.id` (UUID primary key). It doubles as
the identifier for the follow-up file transfer: after this JSON event succeeds, Aspired
streams the file body to `POST https://moonieful.com/portal/api/sync/file/<document_id>/`
using the same file-transfer HMAC scheme as Direction 1's file endpoint (`sign(timestamp,
f'{url}\n{size}')`, `Content-Type: application/octet-stream`, filename in
`X-Sync-Filename`).

`data.direction` is Aspired's own `ClientDocument.direction` value, unchanged —
`to_client` means Aspired (or the admin on Aspired's behalf) sent it toward the client;
`from_client` means the client uploaded it through the Aspired portal.

**Receiver-side implication (Moonieful repo, not yet implemented as of this writing):**
`sync/handlers.py::HANDLERS` on Moonieful's side needs a `document_added` entry whose
handler creates a `Document` row **using `document_id` as that row's own primary key**,
not a freshly generated one — her `inbound_file` view resolves the target purely via
`Document.objects.get(id=document_id)` and 404s if no row exists yet at that id. Until
that handler exists, `dispatch()` treats `document_added` as an unrecognized event type
(logged, ignored, still returns HTTP 200) and the follow-up file POST will 404 against
`inbound_file` every time — Aspired's sender treats that as a failed job (both the
metadata POST and the file POST must return 200 for the job to be marked sent) and
retries on the standard backoff before eventually alerting an admin, rather than silently
dropping the file.

## Field ownership

- **Moonieful owns** (source of truth): email, password hash, contact name, business
  info, intake answers, Moonieful's own stage history.
- **Aspired owns**: website build stage, revisions, maintenance status, Stripe IDs,
  support tickets. `Website.business_type` starts blank (`''`) for every Moonieful
  referral and is never overwritten by sync in either direction — it's set by hand once
  a human knows it.
- **Shared, last-writer-wins by `updated_at`**: documents.

## Idempotency & retries

Every event carries an `event_id`. Both inbound endpoints must check it against their
own audit log (`SyncLog` on both sides) before applying anything, and return
`duplicate: true` rather than re-running the handler on a repeat delivery. Senders
retry on any non-200 response with backoff; the receiver being non-idempotent is what
turns a network blip into duplicate emails, duplicate log rows, or duplicate records.
