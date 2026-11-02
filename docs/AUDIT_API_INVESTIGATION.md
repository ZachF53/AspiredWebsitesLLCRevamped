# Website Audit — Technical Write-up for External (GHL Voice AI) Integration

Investigated 2026-10-30. No code changed. All timings in this document are
measured on this machine against live PSI, not estimated.

---

## 1. Location

**The audit is 100% server-side and fully synchronous.** The browser does
not talk to Google at all. The visitor POSTs an HTML form, the Django
worker blocks for 10–35 seconds while it calls PageSpeed Insights, then
302-redirects to a results page that reads out of the session.

| Concern | File |
|---|---|
| Routes | [public/urls.py:108-114](public/urls.py#L108-L114) |
| Form POST handler | [public/views.py:1438](public/views.py#L1438) `audit()` |
| Results renderer | [public/views.py:1486](public/views.py#L1486) `audit_results()` |
| **The PSI call** | [public/views.py:1607](public/views.py#L1607) `_run_pagespeed_audit()` |
| Issue extraction | [public/views.py:1678](public/views.py#L1678) `_category_issues()` |
| Plain-English copy | [public/views.py:49-98](public/views.py#L49-L98) label/tier/impact dicts |
| Score → tier | [public/views.py:101](public/views.py#L101) `_score_tier()` |
| One-line summary | [public/views.py:1574](public/views.py#L1574) `_audit_summary()` |
| AI review (HTMX partial) | [public/views.py:1727](public/views.py#L1727) `audit_ai_review()` |
| URL field validation | [public/forms.py:329](public/forms.py#L329) `AuditForm` |
| Persistence | [public/models.py:215](public/models.py#L215) `AuditLead` |
| Entry template | [public/templates/public/audit.html](public/templates/public/audit.html) |
| Results template | [public/templates/public/audit_results.html](public/templates/public/audit_results.html) |

The entry template contains **zero JavaScript** — it is a bare
`<form method="post">` with a CSRF token. The only JS anywhere in the flow
is one HTMX `hx-get` on the *results* page that lazily loads the
Claude-written review after the scores already rendered.

### There is a second, independent PSI implementation

[outreach/enricher.py:726](outreach/enricher.py#L726) `_run_pagespeed()`
calls the same API with the same params for lead enrichment. It is a
separate code path with a different timeout (30s vs 45s) and different
parsing. Two things in it are worth knowing before you copy from it:

- [outreach/enricher.py:769](outreach/enricher.py#L769) assigns the
  `best-practices` score into `lead.website_mobile_score`. That field is
  **not a mobile score** and never has been. Do not use it as one.
- [outreach/enricher.py:774](outreach/enricher.py#L774) reads
  `data.get('lheighthouseResult')` — a typo. It is harmless only because
  of the `or data.get('lighthouseResult')` fallback immediately after it.

---

## 2. The PageSpeed call

```python
# public/views.py:38-39
PAGESPEED_API_URL = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
PAGESPEED_TIMEOUT_SECONDS = 45

# public/views.py:1615-1631
params = [
    ('url', url),
    ('strategy', 'mobile'),
    ('category', 'PERFORMANCE'),
    ('category', 'SEO'),
    ('category', 'BEST_PRACTICES'),
    ('category', 'ACCESSIBILITY'),
]
if settings.GOOGLE_PAGESPEED_API_KEY:
    params.append(('key', settings.GOOGLE_PAGESPEED_API_KEY))

response = requests.get(
    PAGESPEED_API_URL,
    params=params,
    timeout=PAGESPEED_TIMEOUT_SECONDS,
)
```

**`strategy=mobile` only. Desktop is never requested.** One call, four
categories. Confirmed in the response: `configSettings.formFactor =
"mobile"`, `emulatedFormFactor = "mobile"`.

Lighthouse version currently being served: **13.6.0**.

Response size is large — **673 KB to 964 KB** of JSON per call across the
runs measured. That matters if you ever proxy it anywhere.

---

## 3. API key

- **Env var:** `GOOGLE_PAGESPEED_API_KEY`
- **Declared:** [settings.py:517](AspiredWebsitesRevamped/settings.py#L517) — `env('GOOGLE_PAGESPEED_API_KEY', '')`
- **Read:** [public/views.py:1623-1624](public/views.py#L1623-L1624) and [outreach/enricher.py:731](outreach/enricher.py#L731)
- **Blanked in tests:** [settings_test.py:130](AspiredWebsitesRevamped/settings_test.py#L130) and `settings_rehearsal.py`
- **Currently set in `.env`:** yes, 39 chars, `AIzaSy…` prefix

### Is it exposed to the browser?

**No.** It is only ever appended to a server-side `requests.get` query
string. It never enters template context, never reaches rendered HTML,
and the audit page ships no JS that could read it.

### Restriction status — measured, not assumed

The calls in this investigation ran from a Windows dev box as a plain
Python process, sending **no `Referer` header** and from a residential IP
that is not a server. They succeeded.

That empirically rules out both restriction types that would break your
integration:

- **Not HTTP-referrer-restricted.** A referrer-restricted key rejects
  requests with no matching `Referer`. These had none and returned 200.
- **Not IP-restricted.** An IP allowlist of the droplets would have
  rejected this machine.

**So the key is effectively unrestricted and will work server-to-server.**
Verify in the GCP console to be certain, and note the security posture:
an unrestricted key is low-value (PSI is free, 25k/day) but should still
be narrowed to the PageSpeed Insights API under *API restrictions*.

**The voice agent must never hold this key.** It calls your endpoint; your
server holds the key. Otherwise the key ends up in GHL's config where you
cannot rotate or audit it.

---

## 4. Response handling

### Fields actually extracted

Everything else in the ~800 KB payload is discarded.

```python
# public/views.py:1654-1673
lighthouse = data.get('lighthouseResult') or {}
categories = lighthouse.get('categories') or {}
audits     = lighthouse.get('audits') or {}

def pct(key):
    cat = categories.get(key) or {}
    score = cat.get('score')
    return round((score or 0) * 100)

scores = {
    'performance':    pct('performance'),
    'seo':            pct('seo'),
    'best_practices': pct('best-practices'),
    'accessibility':  pct('accessibility'),
}
```

Per-category issues come from walking `categories[x].auditRefs`, looking
each id up in `audits`, and keeping an entry only when **all** of these
hold ([public/views.py:1678](public/views.py#L1678)):

- `audit.score` is not `None` **and** `< 0.9`
- `details.type == 'opportunity'` **or** `scoreDisplayMode == 'binary'`
  (this is what excludes pure metrics and informational diagnostics)
- a non-empty `title` exists

Markdown links in the description are flattened with
`re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', ...)`. Results are sorted
worst-score-first and **truncated to 2 per category**.

Note: **LCP is never extracted.** It is present in the response
(`audits['largest-contentful-paint']`) but the current code ignores it.

### Real parsed object

Produced by importing and calling the production
`_run_pagespeed_audit()` directly against `https://www.jonwayne.com`
(a real San Antonio HVAC company). Took **13.47s**.

```json
{
  "scores": {
    "performance": 72,
    "seo": 100,
    "best_practices": 96,
    "accessibility": 84
  },
  "issues_by_category": {
    "performance": [
      {
        "title": "Reduce unused CSS",
        "description": "Reduce unused rules from stylesheets and defer CSS not used for above-the-fold content to decrease bytes consumed by network activity. Learn how to reduce unused CSS.",
        "score": 0
      },
      {
        "title": "Avoid multiple page redirects",
        "description": "Redirects introduce additional delays before the page can be loaded. Learn how to avoid page redirects.",
        "score": 0
      }
    ],
    "seo": [],
    "best_practices": [
      {
        "title": "Browser errors were logged to the console",
        "description": "Errors logged to the console indicate unresolved problems. They can come from network request failures and other browser concerns. Learn more about this errors in console diagnostic audit",
        "score": 0
      }
    ],
    "accessibility": [
      {
        "title": "Elements with an ARIA `[role]` that require children to contain a specific `[role]` are missing some or all of those required children.",
        "description": "Some ARIA parent roles must contain specific child roles to perform their intended accessibility functions. Learn more about roles and required children elements.",
        "score": 0
      },
      {
        "title": "Background and foreground colors do not have a sufficient contrast ratio.",
        "description": "Low-contrast text is difficult or impossible for many users to read. Learn how to provide sufficient color contrast.",
        "score": 0
      }
    ]
  }
}
```

### Scores → plain English

Three layers, all pure string lookup. No AI involved in any of it.

**Layer 1 — tier.** [public/views.py:101](public/views.py#L101):
`>= 90` → `strong`, `>= 50` → `needs-work`, else `critical`.
(`audit_results()` separately defines its own `status_for()` with the
same thresholds but different labels — `good`/`ok`/`bad` — used only for
the score-card CSS class.)

**Layer 2 — per-card impact sentence.** A hardcoded 4×3 table at
[public/views.py:64-98](public/views.py#L64-L98), keyed
`[category][tier]`. For `performance` + `needs-work`:

> "Slow load times are costing you visitors. Most people leave if a site
> takes more than 3 seconds to load."

**Layer 3 — overall one-liner.** `_audit_summary()`
([public/views.py:1574](public/views.py#L1574)):

```python
if any(s < 50 for s in values):   → f'{domain} has critical issues that need immediate attention.'
needs_work = sum(1 for s in values if s < 90)
if needs_work:                     → f'{domain} has {n} area(s) that need(s) attention.'
else:                              → f'{domain} is performing well across all areas.'
```

For the jonwayne.com scores above this returns, verbatim:

```json
{"tier": "needs-work", "text": "jonwayne.com has 2 areas that need attention."}
```

**This is the function you want for the voice summary.** It is already a
single plain-English sentence with no markup, and it has no session or
request dependency — it takes `(url, scores)` and returns a dict.

A fourth, optional layer exists: `audit_ai_review()` sends the scores and
up to 8 issues to Claude for a multi-paragraph narrative. It is HTMX-only,
returns **HTML not JSON**, reads from the session, and is useless for a
voice agent.

---

## 5. Existing endpoint

**There is none.** No route anywhere in the project takes a URL and
returns audit JSON. Verified against `AspiredWebsitesRevamped/urls.py`
and every app's urls module.

The closest existing things are the two public JSON status endpoints,
which are good structural precedents but unrelated in function:
`/api/reviews/status/<uuid>/` and `/api/voice/status/<uuid>/`
([voice/views_public.py:86](voice/views_public.py#L86)).

### Could an external server call the audit today?

**No.** Five independent blockers:

| # | Blocker | Detail |
|---|---|---|
| 1 | **No JSON** | `POST /audit/` returns a 302 to `/audit/results/`, which renders HTML. Audit data is never serialized. |
| 2 | **Session coupling** | Scores live in `request.session['audit_scores']`. An external caller would have to POST, keep the session cookie, then GET and scrape HTML. |
| 3 | **CSRF** | `CsrfViewMiddleware` is global and `audit()` is not `csrf_exempt`. A bare POST gets 403. You would need to GET the page for a token + cookie first. |
| 4 | **Rate limit** | `@ratelimit(key='ip', rate='3/h', method='POST')`. **Three per hour per IP.** Every voice-agent call arrives from the same IP, so the 4th call in an hour fails. This alone kills it. |
| 5 | **No CORS** | `django-cors-headers` is not in `requirements.txt` and no CORS middleware is installed. Irrelevant for server-to-server, fatal for browser JS. |

On CORS, there *is* a hand-rolled precedent to copy if you ever need it:
`cors_post` at [reporting/views.py:74](reporting/views.py#L74), used by
the conversion-tracker endpoints.

---

## 6. Timing — measured

Method: replicated the exact production call (same URL, params, timeout),
plus direct calls to `_run_pagespeed_audit()` itself. Targets were
`aspiredwebsites.com` (control), `jonwayne.com` and
`championacservice.com` (real San Antonio HVAC companies).

### Cold full audit — 4 categories, cache miss

**n = 10 successful cold calls:**

| | seconds |
|---|---|
| min | **10.51** |
| median | **14.54** |
| mean | 16.76 |
| max | **35.51** |

Raw: 10.51, 11.55, 13.45, 13.46, 13.47, 15.60, 15.81, 18.12, 20.07, 35.51

**Nothing came in under 10 seconds on a cold call.** The 35.51s outlier is
within 10s of the 45s timeout.

### Google-side cache hits

| | seconds |
|---|---|
| min | 0.93 |
| median | 1.11 |
| max | 1.33 |

Raw: 0.93, 1.04, 1.07, 1.14, 1.28, 1.33

### Caching — the critical finding

**There is no application-level caching whatsoever.** No `cache.get` in
`_run_pagespeed_audit`, no memoization, no `AuditLead` lookup by URL. Every
form submission is a fresh PSI call. No TTL to speak of because there is
no cache.

**Google caches server-side, but non-deterministically.** Six back-to-back
identical calls, with `fetchTime` exposing which Lighthouse run was served:

```
call 1:  15.81s  fetchTime=2026-10-30T13:24:42.849Z  795KB
call 2:   1.04s  fetchTime=2026-10-30T13:24:42.849Z  795KB   ← cache hit
call 3:  20.07s  fetchTime=2026-10-30T13:24:59.575Z  804KB   ← fresh run, 17s later
call 4:   0.93s  fetchTime=2026-10-30T13:24:42.849Z  795KB   ← serves run #1 again
call 5:   1.28s  fetchTime=2026-10-30T13:24:42.849Z  795KB
call 6:   1.33s  fetchTime=2026-10-30T13:24:59.575Z  804KB   ← serves run #2
```

Google holds **multiple cached Lighthouse runs per URL and serves an
arbitrary one**. Call 3 triggered a completely fresh run 17 seconds after
call 1 had already produced a cacheable result. You cannot predict a hit
and you cannot force one.

**Conclusion: Google's cache is a latency lottery, not a strategy.** Any
sub-second guarantee has to come from a cache you own.

### Does requesting fewer categories help?

Single sample per configuration, same URL:

| categories | seconds | payload |
|---|---|---|
| all 4 | 35.51 | 891 KB |
| PERFORMANCE + SEO | 34.42 | 806 KB |
| PERFORMANCE only | 12.24 | 685 KB |

**Do not read a 3× speedup into this.** The 4-category sample here (35.51s)
is the slowest observation in the entire dataset, and PERFORMANCE-only's
12.24s sits comfortably inside the normal 10.5–20.1s cold range. The
Lighthouse page load dominates, not the audit count. Dropping categories
probably saves a little and definitely shrinks the payload, but it will
**not** get you under 5 seconds. If you want to bank on it, it needs
~10 samples per config.

### Rate limiting, queuing, retries

- **App-level:** `3/h per IP` on `POST /audit/`
  ([public/views.py:1437](public/views.py#L1437)), `block=False` so the
  view renders a friendly form error rather than a 429.
- **Google quota:** 25k queries/day free
  ([settings.py:515](AspiredWebsitesRevamped/settings.py#L515)). Not a
  constraint at your volume.
- **Queuing:** none. The audit runs inline in the request/response cycle
  and ties up a Gunicorn worker for the full 10–35s.
- **Retries: none, anywhere.** `grep` for `HTTPAdapter`/`Retry(`/`urllib3`
  across the whole repo returns nothing. Single attempt, then fail.

The worker-occupancy point is worth dwelling on: with the default worker
count, a handful of concurrent audits can starve the site. That is a
pre-existing issue the voice agent would amplify.

---

## 7. Failure modes

All error shapes below are real captured responses.

| Input | PSI result | Time | Error detail |
|---|---|---|---|
| Nonexistent hostname | HTTP 400 | 18.43s | `Lighthouse returned error: FAILED_DOCUMENT_REQUEST` |
| Malformed string (`notaurl`) | HTTP 400 | 0.56s | `code=400 status=INVALID_ARGUMENT` |
| Real site PSI can't load (`championacservice.com`) | HTTP 400 | 18.72 / 19.15 / 0.55s | same `FAILED_DOCUMENT_REQUEST` family |
| **404 path on a live domain** | **HTTP 200** | 10.51s | **perf score = 1.0 (i.e. 100)** |
| `http://` → `https://` redirect | HTTP 200 | 13.46s | follows it; `requestedUrl` = http, `finalUrl` = https |

Two of these deserve emphasis:

**A 404 page audits as a perfect score.** `https://aspiredwebsites.com/definitely-not-a-page-xyz`
returned HTTP 200 with performance 100. A lightweight error page is fast,
so Lighthouse loves it. **For a voice agent this is the worst failure mode
there is** — a mis-heard or mis-spelled URL yields a confident "your site
scores 100" instead of an error. Guard it by comparing `requestedUrl` to
`finalUrl` and by independently checking the page's HTTP status.

**Real HVAC sites do fail outright.** `championacservice.com` failed all
three attempts. This is not an edge case you can design around; the
endpoint needs a clean "couldn't analyze" path.

Note also that Google caches failures fast — the third 400 came back in
0.55s.

### What the user currently sees

Every failure is surfaced as a Django form error on the audit page.
Verbatim strings from [public/views.py:1632-1652](public/views.py#L1632-L1652):

| Condition | Message |
|---|---|
| `requests.Timeout` (>45s) | "The audit took too long. Try again in a moment, or send us the URL directly at 210-201-0375." |
| `requests.RequestException` | "Couldn't reach the audit service. Please try again in a minute." |
| Any non-200 (400 **and** 429 collapse here) | "We couldn't analyze that URL. Double-check it loads in a browser and try again." |
| Non-JSON body | "Got an unexpected response from the audit service." |
| Rate limited (4th in an hour) | "You've run too many audits in the last hour. Please try again later or call us at 210-201-0375 for a manual review." |
| Invalid URL (form validation, pre-PSI) | "Please enter a valid URL, e.g. https://yourbusiness.com" |

Quota exhaustion is indistinguishable from an unreachable URL — both are
non-200 and both produce the "couldn't analyze that URL" message. If you
care about telling those apart you need to branch on `status_code` and log
the `error.message`.

---

## 8. Recommendation

### Is a synchronous call viable?

**Not for a cold URL. Median 14.5s against a 3–5s budget is not a tuning
problem, it's an architecture mismatch.** You cannot fix this by trimming
categories (measured above) or raising a timeout.

**But synchronous is viable on a warm cache** — a hit returns in
single-digit milliseconds from Redis. So the answer is a **cache-first
hybrid**, not a straight choice between sync and async:

```
Agent calls endpoint
  ├─ URL in our Redis cache  → return full result inline (~5ms)   ✅ sync
  └─ cache miss              → return {"status":"pending","job_id":...} in <100ms
                               agent says "I'll text that over in a minute"
                               Celery runs PSI, result delivered by SMS/webhook
```

That gets you a real answer on the call whenever the URL has been seen
before, and a graceful out when it hasn't.

### The lean endpoint

Not yet written — you said don't change code. This is the shape I'd
propose, following the repo's existing conventions
(`voice/ghl_webhook.py` for the secret, `voice/views_public.py` for the
JSON response).

```python
# public/views_audit_api.py  (PROPOSED — not created)
import hmac
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache
from django_ratelimit.decorators import ratelimit


def _secret_ok(provided):
    """Constant-time compare, fails closed on an unset secret —
    same posture as voice/ghl_webhook.py:_secret_ok."""
    expected = (getattr(settings, 'AUDIT_API_SECRET', '') or '').strip()
    if not expected:
        return False
    return hmac.compare_digest(str(provided or ''), expected)


@csrf_exempt
@ratelimit(key='post:url', rate='30/h', method='POST', block=False)
def audit_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    if not _secret_ok(request.headers.get('X-Audit-Secret')):
        return JsonResponse({'error': 'forbidden'}, status=403)

    url = (request.POST.get('url') or '').strip()
    if not url:
        return JsonResponse({'error': 'url required'}, status=400)

    key = f'audit_api:{_normalize(url)}'
    hit = cache.get(key)
    if hit:
        return JsonResponse({'status': 'ready', 'cached': True, **hit})

    from public.tasks import run_audit_for_voice
    job = run_audit_for_voice.delay(url)
    return JsonResponse({'status': 'pending', 'job_id': job.id}, status=202)
```

Response contract — exactly the four fields you asked for:

```json
{
  "status": "ready",
  "cached": true,
  "url": "https://www.jonwayne.com",
  "performance_score": 72,
  "lcp_seconds": 5.0,
  "lcp_ms": 4952,
  "mobile_friendly": true,
  "summary": "jonwayne.com has 2 areas that need attention."
}
```

Where each field comes from:

- **`performance_score`** — `round(categories['performance']['score'] * 100)`.
  Reuse `_run_pagespeed_audit()`'s `pct()`.
- **`lcp_ms` / `lcp_seconds`** — `audits['largest-contentful-paint']['numericValue']`.
  Measured real values: 1052 ms (aspiredwebsites.com), 4952 ms (jonwayne.com).
  **New extraction** — the current code does not pull LCP.
- **`summary`** — call the existing `_audit_summary(url, scores)`. It
  already returns one clean sentence and has no request dependency. Free.
- **`mobile_friendly`** — see the warning below.

### ⚠️ "Mobile-friendly yes/no" has no source in the PSI response

I probed for it specifically. In **Lighthouse 13.6.0**, all four
mobile-friendliness audits are **absent from the response entirely**:

```
viewport        ABSENT
content-width   ABSENT
font-size       ABSENT
tap-targets     ABSENT
```

They are not merely unscored, they are gone — and they do not appear in
the SEO category's `auditRefs` either. The original `viewport_score: null`
I saw was the absence, not a null score. There is no mobile-friendly
boolean to read.

Three ways to produce one honestly, best first:

1. **Fetch the page and check for the viewport meta tag yourself.** A
   plain `requests.get` + regex for `<meta name="viewport">` takes
   ~200–500 ms, is deterministic, and answers the question that actually
   matters for an old HVAC site: was this built for phones at all. Fast
   enough to run inline even on a cache miss.
2. **Derive from the mobile run.** The whole audit is already
   `strategy=mobile`, so define e.g. `perf >= 50 and CLS <= 0.1`. Cheap,
   but it is a performance claim wearing a mobile-friendliness label.
3. **CrUX field data.** `loadingExperience.overall_category` returned
   `SLOW` for jonwayne.com. Real user data, but it only exists for sites
   with enough traffic — most small HVAC sites will have none.

I'd do **(1)** for the boolean and keep **(3)** as an extra field when
present. Whatever you pick, do not reuse
`enricher.website_mobile_score` — that is the best-practices score
mislabeled.

### Async pattern — yes, you need it

For cache misses there is no alternative. Components:

- **Celery is already running** in production under supervisor
  (`aspiredwebsites-celery`), and `public/tasks.py` already exists as a
  home for the task. Fire with `.delay()` for immediate execution — this
  is not the days-ahead scheduling that needs a beat sweep.
- **Delivery back to the caller.** Two options: POST to a GHL inbound
  webhook, or SMS the prospect directly. Note that **no Twilio send
  helper exists in this codebase yet** — `TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` are in settings but nothing
  sends SMS. That is net-new work, and A2P consent applies: you can only
  text the prospect if they consented on the call, per the call-first
  consent rule.
- **Job status polling** as a fallback: `GET /api/audit/status/<job_id>/`,
  mirroring `/api/voice/status/<uuid>/`.

### Caching strategy — the real unlock

Redis is already configured and already resilient
(`core.cache_backend.ResilientRedisCache`, degrades to per-process on an
outage rather than 500ing). So this is nearly free:

- **Key** on a normalized URL — lowercase host, strip `www.`, strip
  trailing slash, drop query/fragment. `_domain_of()` at
  [public/views.py:30](public/views.py#L30) already does most of this.
- **TTL 24 hours**, or even 7 days. A site's Lighthouse scores do not move
  hour to hour — jonwayne.com returned 72/73/75 across three runs, which
  is Lighthouse's own run-to-run noise, not real change. A day-old score
  is as true as a fresh one for a sales conversation.
- **Pre-warm.** This is what makes sync actually work: when a lead is
  created with a website, enqueue the audit then. By the time anyone calls
  them, the cache is warm and the voice agent answers in milliseconds. You
  already have the enrichment pipeline doing a PSI call per lead
  ([outreach/enricher.py:726](outreach/enricher.py#L726)) — writing that
  result into the shared cache would warm it at zero marginal cost.
- **Also cache failures** (shorter TTL, ~1h) so a dead site does not cost
  18 seconds on every call.

### One scoping note

Your documented cold-outreach entry point is **HVAC businesses with no
website at all** (decided 2026-10-10; 148 leads in the dev DB). An audit
endpoint is inapplicable to that segment by definition — there is nothing
to audit. This feature serves a *different* audience: inbound callers and
has-website prospects. Worth being explicit about which segment the voice
agent is working before building the integration, since the pre-warm
strategy above only pays off for leads that have a website on file.

---

## Appendix — unrelated discrepancy found

[public/tests.py:192-195](public/tests.py#L192-L195) documents, in its own
docstring, that the CLAUDE.md-specified audit→Lead conversion is **not
implemented**:

> "(The CLAUDE.md spec for Lead conversion on audit_tool is NOT YET
> implemented in views.audit_results — see commit notes; this test pins
> the actual current behaviour.)"

Email capture creates an `AuditLead` row only. No `Lead(source='audit_tool')`
is created, so audit-tool signups do not appear in the CRM pipeline.
CLAUDE.md's "AuditLead Model" section describes the conversion as live.
Flagging it; not acting on it.
