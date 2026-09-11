# Moonieful ↔ Aspired Websites Sync Contract (v1)

This is the one written reference for the wire format both sides implement against.
It exists because the two `sync` apps live in separate repos, were built months apart,
and drifted apart from each other with nothing to catch it. **If you change this
contract, update the copy in both repos and the code on both sides in the same change.**

Moonieful repo: `sync/bundle.py`, `sync/security.py`, `sync/transport.py`,
`sync/handlers.py`, `sync/views.py`.
Aspired repo: `sync/security.py`, `sync/handlers.py`, `sync/views.py`,
`sync/signals.py`, `sync/management/commands/run_sync.py`.

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

## Direction 2 — Aspired → Moonieful (status only)

`POST https://moonieful.com/portal/api/sync/inbound/`:

```json
{
  "schema_version": 1,
  "source_site": "aspired",
  "event_type": "stage_changed | maintenance_activated",
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
