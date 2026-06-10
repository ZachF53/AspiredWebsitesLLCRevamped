# Onboarding & Checkout Refactor — Full Spec

> Owner: Zachery Long. Last updated: 2026-06-07.
> Status: SPEC ONLY — no code written. Approval required before any build phase begins.
> Goal: replace the current admin-only invoice flow with a hybrid that supports
> self-checkout for maintenance + social products, keeps the admin path for
> web design, and gives every customer a product-tailored onboarding wizard.

---

## 1. Scope of this refactor

### What's changing
- Pricing page gets two checkout paths (admin-invoiced design vs. self-served maintenance/social)
- New custom Schedule-a-Call page with Google Calendar API integration (no third-party scheduler)
- Custom Stripe checkout flow (Stripe API + Elements only — every screen on aspiredwebsites.com)
- Account creation via magic-link password setup after payment
- Product-tailored onboarding wizards with auto-save, skip-buttons, resume-where-you-left-off
- Setup To-Do widget on client portal with vault credential auto-completion
- Subscription management UI (cancel, upgrade, downgrade, cards, invoices) — all custom
- Vault enhancement: grouped dropdown for credential types
- New $50-off-first-year hosting move-over upsell at maintenance checkout

### What's NOT changing
- Existing admin invoice flow (kept as-is for web design)
- Stripe webhook handlers for `checkout.session.completed`, `invoice.paid`, etc.
- Existing IntakeResponse for web design intake
- Vault encryption (PIN-derived AES-256-GCM, per-process server key fallback)
- DigitalOcean droplet provisioning for web design clients (uses `aspired-base-v2` snapshot)
- Existing onboarding for clients we built sites for

### What's marked TODO and explicitly deferred
- Google Business Profile auto-create + manager-add automation
- Google Analytics 4 auto-create + admin-grant automation
- Google Search Console auto-verify (DNS TXT) + access-grant automation
- These appear as Yes/No questions in onboarding today, route to "We'll set it up for you" placeholder copy on No

---

## 2. Phase plan

Eight phases. Each ships independently. Each is testable in isolation.

| Phase | Scope | Why this order |
|---|---|---|
| **1. Vault enhancement** | Add `credential_type` dropdown + `custom_label` field. Backfill existing creds as "Other". Admin can edit type per credential. | Foundation for the To-Do auto-completion in Phase 3. Small, isolated. |
| **2. Onboarding wizard framework** | `Onboarding` + `OnboardingResponse` models. Wizard shell (welcome screen, sections, breadcrumbs, progress bar, auto-save, skip button). NO product-specific question logic yet — just the engine. | Engine first, content second. |
| **3. Setup To-Do widget** | `SetupTodo` model. Sidebar widget with count badge. Centered modal. Auto-completion via vault credential type matching. Reminder emails day 3/7/14. | Needs Phase 1 + 2 done. |
| **4. Maintenance + Social onboarding flows** | Plug the actual question registries into the Phase 2 wizard. Conditional sections. Welcome screen content per product. | Pure content/config, no new infrastructure. |
| **5. Pricing page redesign + custom checkout** | Restructure cards (web design = schedule a call · maintenance/social = buy now · hosting = info-only with upsell). Build the full custom checkout flow with Stripe Elements. | Self-contained — doesn't depend on onboarding being live (a fresh customer just goes to a placeholder if onboarding isn't ready yet). |
| **6. Account creation + magic-link password setup** | New `User.password_setup_required` flag + token model. Webhook hooks. Email template. Set-password page. | Glue between checkout and onboarding. |
| **7. Subscription management UI** | Manage Billing hub in client portal. Cancel / change-plan / add-card / remove-card / past-invoices. Stripe Customer Portal replaced screen-for-screen. | Doesn't block anything else — can ship after checkout is live. |
| **8. Schedule-a-Call calendar system** | `AvailabilityWindow` model. Google Calendar OAuth + sync. Time-slot picker UI. Hold + confirm logic. | Largest single piece. Can ship in parallel with Phase 7. |

**Optional Phase 9 (later):** Google Business Profile / Analytics / Search Console auto-create automations.

---

## 3. Pricing page redesign

### Card layout — current vs. proposed

**Current:** All cards show "Contact Us" → goes through admin.

**Proposed:** Mixed by product type.

| Card | CTA button | Where it goes |
|---|---|---|
| Web Design — Essential ($2,500) | "Schedule a Call" | New `/design/schedule/` page |
| Web Design — Premium ($4,500) | "Schedule a Call" | Same |
| Maintenance — Essentials ($299/mo) | "Buy Now" | Custom checkout, mode=subscription |
| Maintenance — Growth ($599/mo) | "Buy Now" | Same |
| Maintenance — Dominant ($1,199/mo) | "Buy Now" | Same |
| Social Media — Basic ($399/mo) | "Buy Now" | Same |
| Social Media — Standard ($699/mo) | "Buy Now" | Same |
| Social Media — Full ($999/mo) | "Buy Now" | Same |
| Hosting | (no button — info card below the grid) | N/A |

### Feature bullets — additions

Each Web Design card adds:
```
✓ Hosting included — first year free, then $150/year
```

Each Maintenance card adds:
```
✓ Optional hosting move-over — $50 off year one ($100, then $150)
```

Each Social Media card gets a "Reply / DM handling" bullet:
```
Basic:     ✗ Replies & DMs — you handle them
Standard:  ✓ Reply & DM triage — we flag, you respond
Full:      ✓ Full reply & DM management
```

Each Social Media card also gets a posts-per-channel bullet:
```
Basic:     ✓ 8 posts/month per channel × 2 channels  = 16 posts
Standard:  ✓ 12 posts/month per channel × 3 channels = 36 posts
Full:      ✓ 16 posts/month per channel × 5 channels = 80 posts + stories/reels
```

### Hosting info section (full-width, below the grid)

```
┌────────────────────────────────────────────────────────────────────────┐
│  HOSTING                                                                │
│                                                                         │
│  If we build your website, hosting is included — first year free, then  │
│  $150/year on your build's anniversary.                                 │
│                                                                         │
│  If we maintain your existing site, you can move your hosting to our    │
│  servers for $50 off the first year ($100 instead of $150). Renews at   │
│  full price after year one. Lets us patch security issues faster and    │
│  keeps everything we manage in one place.                               │
│                                                                         │
│  Already a maintenance client?  → Opt in from your portal               │
│  Not yet?                       → Choose a maintenance plan above       │
│                                   (hosting move-over offered as a       │
│                                    one-click upsell at checkout)        │
└────────────────────────────────────────────────────────────────────────┘
```

### Files touched
- `public/templates/public/pricing.html` (rewrite card grid + add hosting info section)
- `core/static/css/main.css` (style updates for new bullet types)

### Open questions
- Confirm posts-per-channel cadence (8/12/16) — this is what gets committed to in writing on the pricing page
- Confirm the "Already a maintenance client → Opt in from your portal" wording

---

## 4. Schedule-a-Call page (`/design/schedule/`)

### Purpose
Web design path. Replaces the generic Contact Us for web design specifically. Lets a prospect schedule a kickoff call AND opt into add-on maintenance/social plans with 10% off first month.

### Page structure

```
┌── /design/schedule/ ─────────────────────────────────────────────────┐
│                                                                       │
│  [Hero block — "Let's talk about your build"]                         │
│                                                                       │
│  [Calendar widget — custom, Google Calendar API]                     │
│    - Shows admin's real available windows                            │
│    - 30-min slots                                                    │
│    - Customer picks slot → tentative hold                            │
│                                                                       │
│  [Contact form]                                                       │
│    - Name (required)                                                 │
│    - Email (required)                                                │
│    - Phone (optional)                                                │
│    - Business name (required)                                        │
│    - Website (if existing) (optional)                                │
│    - Build type (Essential / Premium / Not sure) (required)          │
│    - "What are you trying to build?" textarea (required)             │
│                                                                       │
│  [Add-on opt-in — collapsible "Save 10% on your first month"]        │
│    ☐ Maintenance Essentials  ($269 first month, then $299/mo)        │
│    ☐ Maintenance Growth      ($539 first month, then $599/mo)        │
│    ☐ Maintenance Dominant    ($1,079 first month, then $1,199/mo)    │
│    ☐ Social Basic            ($359 first month, then $399/mo)        │
│    ☐ Social Standard         ($629 first month, then $699/mo)        │
│    ☐ Social Full             ($899 first month, then $999/mo)        │
│                                                                       │
│  [Submit — locks slot + creates Lead with opt-ins recorded]          │
└───────────────────────────────────────────────────────────────────────┘
```

### Calendar system (deferred to Phase 8, spec'd here for completeness)

**New models:**
```
AvailabilityWindow
  ├─ day_of_week        0-6 (Mon-Sun)
  ├─ start_time         time
  ├─ end_time           time
  ├─ timezone           str (America/Chicago default)
  └─ active             bool

ScheduledCall
  ├─ lead               FK to outreach.Lead
  ├─ google_event_id    str
  ├─ starts_at          datetime
  ├─ ends_at            datetime
  ├─ status             held | confirmed | cancelled | completed
  └─ created_at         datetime
```

**Admin UI:** `/admin-dashboard/schedule/availability/` — manage windows
**Customer UI:** Embedded calendar widget on `/design/schedule/`

**Google Calendar integration:**
- OAuth flow for admin to connect their calendar
- Read events from connected calendar to know what's actually busy (beyond defined windows)
- Write event back to calendar when slot is confirmed
- Webhook (or polled sync) to know if admin cancels/moves in Google

**Hold logic:**
- Customer picks slot → 15-min hold on `ScheduledCall(status=held)`
- Submits form → `status=confirmed`, Google event created
- Holds that aren't confirmed in 15 min auto-expire (Celery beat task)

### Lead creation on submit
```
Lead.create(
    source='schedule_call',
    firm_name=<business_name>,
    attorney_name=<name>,
    email=<email>,
    phone=<phone>,
    website=<website>,
    inquiry_text=<"What are you trying to build?">,
    tags=<"build_type:essential" or premium etc>,
    opted_in_addons=<JSON list of slugs they checked>,
    opted_in_addons_at=<now>,
    scheduled_call_id=<FK to ScheduledCall>,
)
```

### Opt-in expiry rule
- **Never auto-expires.** Admin honors at invoice time (default checkbox checked).
- Stale-flag pill at 90 days of admin inactivity on the Lead (visual nudge only).

### Files to create (Phase 4 + 8)
- `public/views.py` → `design_schedule(request)`
- `public/templates/public/design_schedule.html`
- `clients/models.py` → `AvailabilityWindow`, `ScheduledCall`
- `admin_dashboard/views.py` → `availability_manage`, `availability_add`, `availability_delete`
- `admin_dashboard/templates/admin_dashboard/availability.html`
- `core/static/js/calendar_picker.js` — custom slot picker
- `clients/google_calendar.py` — OAuth + sync wrapper

---

## 5. Self-service checkout (custom Stripe Elements UI)

### Scope
Maintenance + Social Media checkouts ONLY. Web Design always goes through admin invoice. Hosting is upsold inline during maintenance checkout, not a standalone product.

### Why custom (not Stripe Checkout)
Decision: all UI on aspiredwebsites.com. Stripe is API-only. We use Stripe Elements (the iframe-embedded secure card field) so PCI scope stays minimal — card numbers never touch our server — but the page wrapping it is ours.

### Checkout flow

```
PRICING PAGE
   │
   │ [Buy Now] on a maintenance or social tier
   ▼
/checkout/<tier_slug>/
   │
   ├── Plan summary (price, what's included)
   ├── Email input (required)
   │     - On blur: AJAX check if existing user
   │     - If yes: "Sign in to continue" prompt
   │     - If no: continues as guest
   ├── (Maintenance only) Hosting move-over upsell
   │     ☐ Add hosting move-over — $50 off year one ($100)
   │     [Total updates live: subscription line + one-time hosting line]
   ├── Stripe Elements card field
   ├── Billing address (Stripe Address Element)
   ├── Total breakdown
   └── [Pay $X.XX] button
       │
       │ POST to /checkout/<tier_slug>/confirm/
       │   - Create Stripe Customer (if new)
       │   - Create Stripe Subscription with payment method
       │   - (If hosting move-over checked) add one-time invoice item
       │   - Handle 3DS / SCA confirmation
       ▼
   /checkout/<tier_slug>/success/
       │
       │ Webhook fires: checkout.session.completed
       │   - Creates User in pending_password state
       │   - Triggers magic-link email
       │   - Creates Onboarding(product_type=<tier_category>, tier=<tier_slug>)
       │   - If hosting upsell: queues HostingMoveTodo for later
       ▼
   Customer sees: "Check your email — we sent you a link to set your password
   and start your setup."
```

### Stripe API calls

| When | Call |
|---|---|
| Customer types email | `Customer.list(email=...)` to check existing |
| Customer submits payment | `PaymentMethod.attach()` then `Subscription.create()` with `default_payment_method` |
| If hosting upsell checked | Add `InvoiceItem.create()` for one-time $100 (or $100 - any pro-ration) BEFORE the subscription's first invoice finalizes |

### Files to create
- `billing/checkout_views.py` → `checkout_page`, `checkout_confirm`, `checkout_success`
- `billing/templates/billing/checkout.html`
- `billing/templates/billing/checkout_success.html`
- `core/static/js/checkout.js` — Stripe Elements init + form submission
- `billing/webhooks.py` → extend existing handler for the new flow

### CSP considerations
Stripe Elements requires:
```
script-src 'self' https://js.stripe.com
frame-src https://js.stripe.com https://hooks.stripe.com
```
Add these to the existing CSP for the checkout pages only (per-view middleware, not global).

### Open questions
- Address Element collection — required or optional? (Stripe Tax recommends required for accurate tax calculation)
- Tax — enable Stripe Tax or skip for now? (Most B2B services in TX/GA are non-taxable, but check with an accountant)
- Refund policy on the checkout page — link to a `/refund-policy/` page

---

## 6. Account creation + magic-link password setup

### New flow

```
Stripe webhook: checkout.session.completed
  │
  ├── Look up User by email
  │   ├── EXISTS → attach purchase to that User, fire onboarding directly
  │   └── NEW    → create User with:
  │                  - email
  │                  - random unusable password
  │                  - is_active = True
  │                  - password_setup_required = True
  │                Create PasswordSetupToken (UUID, 7-day expiry)
  │                Send magic-link email: /set-password/<token>/
  │
  ├── Create Onboarding(user=<user>, product_type=..., tier_slug=...)
  ├── (If hosting move-over) queue HostingMoveTodo
  └── Send purchase receipt email
```

### Magic-link email
```
Subject: Welcome to Aspired Websites — set your password

Hi {first_name},

Thanks for your purchase! Click below to set your password and start
your setup walkthrough. The link expires in 7 days.

[Set Your Password]   ← /set-password/<token>/

Once your password is set, you'll be guided through a short onboarding
({estimate_minutes} minutes) so we can get to work right away.

— Zachery
```

### Set-password page
```
/set-password/<token>/
  ├── Token validation (exists, not expired, not consumed)
  ├── Password + confirm password fields
  ├── Submit:
  │   - hash + set password
  │   - mark password_setup_required = False
  │   - consume token
  │   - log user in
  │   - redirect to /onboarding/<product_type>/
```

### New model
```
PasswordSetupToken
  ├─ user           FK to User
  ├─ token          UUID (unique)
  ├─ created_at     datetime
  ├─ expires_at     datetime (created_at + 7 days)
  └─ consumed_at    datetime (null until used)
```

### User model addition
```
class User(AbstractUser):
    ...existing...
    password_setup_required = models.BooleanField(default=False)
```

### Files to create
- `clients/views.py` → `set_password(request, token)`
- `clients/templates/clients/set_password.html`
- `clients/models.py` → `PasswordSetupToken`
- `clients/emails.py` → `send_password_setup_email(user)`
- `clients/migrations/00XX_password_setup.py`

---

## 7. Onboarding wizard framework

### Concept
One reusable wizard engine. Product-specific question registries plug in. Same UI, same auto-save, same progress logic for every product type.

### Models

```
class Onboarding(models.Model):
    user             = models.ForeignKey(User, on_delete=models.CASCADE)
    product_type     = models.CharField(max_length=20, choices=[
        ('maintenance', 'Maintenance'),
        ('social_media', 'Social Media'),
        ('website_design', 'Website Design'),
    ])
    tier_slug        = models.CharField(max_length=50)  # matches ServiceTier.slug
    welcome_seen     = models.BooleanField(default=False)
    started_at       = models.DateTimeField(auto_now_add=True)
    completed_at     = models.DateTimeField(null=True, blank=True)
    last_section     = models.CharField(max_length=10, blank=True)   # e.g. "M2"
    last_question_idx = models.IntegerField(default=0)

    class Meta:
        unique_together = ('user', 'product_type', 'tier_slug')


class OnboardingResponse(models.Model):
    onboarding   = models.ForeignKey(Onboarding, on_delete=models.CASCADE,
                                     related_name='responses')
    question_key = models.CharField(max_length=80)
    value        = models.TextField(blank=True)
    skipped      = models.BooleanField(default=False)
    saved_at     = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('onboarding', 'question_key')
```

### Question registry (in code, not DB)

Same pattern as `BLANK_BUILDER_FIELDS` from the brief generator:

```python
ONBOARDING_QUESTIONS = {
    'maintenance': [
        {'section': 'M1', 'section_title': 'About your current site',
         'key': 'current_site_url', 'label': 'Site URL we will maintain',
         'type': 'text', 'placeholder': 'https://...', 'required': True,
         'skip_allowed': False},
        # ... (full list in section 8)
    ],
    'social_media': [
        # ... (full list in section 9)
    ],
}

CONDITIONAL_RULES = {
    'maintenance': {
        # Section M5 (migration) only shown if hosting_moveover_purchased
        'show_section_if': {
            'M5': lambda onboarding: onboarding.user.profile.hosting_moveover_purchased
        },
    },
    'social_media': {
        # Section S3 (reply/DM policy) only for Standard + Full
        'show_section_if': {
            'S3': lambda onboarding: onboarding.tier_slug in (
                'social-standard', 'social-full')
        },
        # Section S4 skipped if existing website client
        'skip_section_if': {
            'S4': lambda onboarding: onboarding.user.client_profile.intake_response.completed
        },
    },
}
```

### URL routes

```
/onboarding/                          → routes to user's active onboarding
/onboarding/<product_type>/welcome/   → welcome screen
/onboarding/<product_type>/<section>/ → wizard step
/onboarding/<product_type>/save/      → AJAX endpoint for per-question save
/onboarding/<product_type>/skip/      → AJAX endpoint for skip
/onboarding/<product_type>/complete/  → completion screen
```

### Wizard UI components

**Welcome screen:**
```
┌──────────────────────────────────────────────────────────────────────┐
│  Welcome to your {product_type} setup                                │
│                                                                       │
│  We're going to ask you about: {section_overview_list}               │
│  Why: {one_paragraph_explanation}                                    │
│                                                                       │
│  ⚠ Every question is required UNLESS you check "Skip this           │ ← bold red
│    question." Each answer auto-saves the moment you click out of    │
│    the field, so you can leave and come back anytime — your work    │
│    won't be lost.                                                    │
│                                                                       │
│  Estimated time: about {estimate} minutes                            │
│                                                                       │
│                                              [Start →]               │
└──────────────────────────────────────────────────────────────────────┘
```

**Step screen:**
```
┌── Onboarding ──── Section {N} of {total}: {section_title} ───────────┐
│                                                                       │
│  Breadcrumbs:  ✓ M1   ✓ M2   ● M3   ○ M4                            │
│                                                                       │
│  Progress: ████████████░░░░░░░░░░░░░░░░  {answered + skipped}%       │
│                                                                       │
│  {question label}                                                     │
│  {input field}                          [Skip this question]         │
│                                                                       │
│  {question label}                                                     │
│  {textarea}                             [Skip this question]         │
│                                                                       │
│                                  [← Back]   [Save & continue →]      │
└───────────────────────────────────────────────────────────────────────┘
```

**Auto-save behavior:**
- Input blur → POST to `/onboarding/<product_type>/save/` with `{question_key, value}`
- Skip click → POST to `/onboarding/<product_type>/skip/` with `{question_key}`
- Both update the progress bar instantly via JS (no full page reload)
- "Save & continue" button just navigates — there's nothing to save (already saved)

**Progress math:**
```
total_visible = count of questions in sections visible to this user/tier
answered      = count of OnboardingResponse where value != '' and skipped == False
skipped       = count of OnboardingResponse where skipped == True
percentage    = (answered + skipped) / total_visible * 100
```

**Resume-where-you-left-off:**
- Every wizard view updates `Onboarding.last_section` + `last_question_idx`
- Visiting `/onboarding/` while in progress → 302 to the exact section + scrolls to the last-touched question

### Files to create
- `clients/models.py` → `Onboarding`, `OnboardingResponse`
- `clients/onboarding_registry.py` → `ONBOARDING_QUESTIONS`, `CONDITIONAL_RULES`
- `clients/onboarding_views.py` → `welcome`, `wizard_step`, `save`, `skip`, `complete`
- `clients/templates/clients/onboarding/welcome.html`
- `clients/templates/clients/onboarding/step.html`
- `clients/templates/clients/onboarding/complete.html`
- `core/static/js/onboarding_wizard.js` — auto-save + progress bar
- `core/static/css/main.css` — wizard styles
- Migration adding the two models

---

## 8. Maintenance onboarding — full question registry

### Welcome screen content
```
Welcome to your Maintenance plan setup.

We're going to ask you about:
  • Your current site (4 questions)
  • Access we'll need (7 questions)
  • Site health (5 questions)
  • How we'll work together (4 questions)
  • Migration details (11 questions — only if you opted to move
    your hosting to us)

This helps us protect what you have, fix what's broken, and never
break what's working.

⚠ Every question is required UNLESS you check "Skip this question."
Each answer saves the moment you click out — leave and come back
anytime, your work won't be lost.

Estimated time: about 12 minutes (18 if you're moving hosting).
```

### Section M1 — About your current site (4 questions)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `current_site_url` | Site URL we will maintain | text | No |
| `current_platform` | Platform it's built on | select (WordPress / Squarespace / Wix / Shopify / Webflow / Custom HTML / Other) | No |
| `current_site_age` | Roughly how old is the site? | select (under 1 year / 1-3 yrs / 3-7 yrs / over 7 yrs / not sure) | Yes |
| `accepts_payments` | Does the site accept payments? | bool yes/no | No |

### Section M2 — Access we'll need (7 questions)

Each question is the same shape: dropdown of `I'll share / I don't have it / Don't know what this is`. Routes to vault if "I'll share."

| Key | Label | Vault credential type | Skip? |
|---|---|---|---|
| `access_admin_login` | Site admin login (WordPress, Shopify, etc.) | wordpress_admin/shopify_admin/etc. | No |
| `access_hosting_panel` | Hosting control panel (cPanel, hosting login) | cpanel/hosting_panel | No |
| `access_domain_registrar` | Domain registrar | domain_registrar | No |
| `access_email_workspace` | Email accounts on this domain (Google Workspace, etc.) | email_workspace | Yes |
| `access_google_analytics` | Google Analytics — Yes I have it / No (we'll set it up) | google_analytics | No |
| `access_google_search_console` | Google Search Console — Yes / No (we'll set it up) | google_search_console | No |
| `access_payment_processor` | Payment processor — only shown if `accepts_payments == yes`. Options: Read-only / No access at all | payment_processor | No |

### Section M3 — Site health snapshot (5 questions)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `site_known_issues` | Anything currently broken or annoying? | textarea | Yes |
| `site_recent_changes` | Anything changed in the last 30 days? | textarea | Yes |
| `site_backup_today` | Backup schedule today — does anyone back it up? | select (Yes — describe / No / Not sure) | Yes |
| `site_past_incidents` | Any known security issues or past incidents? | textarea | Yes |
| `site_most_important` | Most important page or feature on the site | text | No |

### Section M4 — How we'll work together (4 questions)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `approval_workflow` | Approval workflow | select (Approve every change / Carte blanche on routine, ask for big changes / Full carte blanche) | No |
| `emergency_contact` | Emergency contact name + phone (for site-down at 2am) | text | No |
| `preferred_contact_method` | Best contact for non-urgent | select (Email / Slack / Portal messages / SMS) | No |
| `content_update_cadence` | Content update cadence expected | select (As-needed / Weekly digest / Monthly digest) | No |

### Section M5 — Migration details (11 questions, only shown if hosting move-over was purchased)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `migration_preferred_window` | Preferred migration window (low-traffic day/time) | text | No |
| `migration_acceptable_downtime` | Acceptable downtime | select (Minutes / A few hours / As long as it takes to do it right) | No |
| `migration_source_access` | Source server access | select (Yes share via vault / Same as hosting panel above / Need help getting it) | No |
| `migration_db_size` | Rough database size + type | select (Small under 100MB / Medium 100MB-1GB / Large over 1GB / Don't know) | Yes |
| `migration_custom_code` | Custom plugins, integrations, or scripts we should know about? | textarea | Yes |
| `migration_cdn` | CDN currently in use | select (None / Cloudflare / Fastly / Other / Don't know) | Yes |
| `migration_cron_jobs` | Any cron jobs or scheduled scripts running? | textarea | Yes |
| `migration_email_hosting` | Where are the domain's email accounts hosted? | select (Google Workspace / Microsoft 365 / Hosted by current host / Other / None) | No |
| `migration_ssl_type` | SSL certificate today | select (Let's Encrypt / Paid cert / Not sure) | Yes |
| `migration_subdomains` | Subdomains we should know about (blog.x.com, store.x.com) | textarea | Yes |
| `migration_webhooks` | Webhooks pointing at the site / APIs the site consumes that might break on URL change | textarea | Yes |

### Section completion triggers Setup To-Do creation

When the onboarding `complete()` view fires, walk the responses:

| Response | Action |
|---|---|
| Any `access_*` response set to "I'll share" | Create SetupTodo with `credential_type` matching the question |
| `access_google_analytics == 'No (we'll set it up)'` | Mark GA auto-create task TODO (Phase 9) |
| `access_google_search_console == 'No (we'll set it up)'` | Mark GSC auto-create task TODO (Phase 9) |
| `migration_*` filled | Create internal admin task for migration prep |

---

## 9. Social Media onboarding — full question registry

### Welcome screen content (varies slightly by tier)
```
Welcome to your Social Media setup.

We're going to ask you about:
  • Your social channels (about {N} per channel — you have {channel_count})
  • Brand voice & content strategy
  {if Standard or Full:}
  • Reply & DM policy
  {endif}
  {if no IntakeResponse on file:}
  • Brand assets (logo, colors, photos)
  {endif}
  • Upcoming campaigns

The more specific you are, the more on-brand we can be from day one.

⚠ Every question is required UNLESS you check "Skip this question."
Each answer saves the moment you click out — leave and come back
anytime.

Estimated time: about {15 / 22 / 30} minutes
```

### Section S1 — Per-channel info (7 questions × `channel_count`)

`channel_count` = 2 / 3 / 5 based on tier. Each slot has:

| Key (templated) | Label | Type | Skip? |
|---|---|---|---|
| `channel_<N>_platform` | Channel {N} — platform | select (FB / IG / LinkedIn / X / TikTok / YouTube / Pinterest / Threads / Other) | No |
| `channel_<N>_handle` | Account URL or handle | text | No |
| `channel_<N>_status` | Status | select (Active — posting / Has account, dormant / Need to create) | No |
| `channel_<N>_followers` | Approximate follower count | text | Yes |
| `channel_<N>_best_post` | Best-performing post (link or screenshot description) | textarea | Yes |
| `channel_<N>_worst_post` | Worst experience / cringe post (link or description) | textarea | Yes |
| `channel_<N>_access` | How will you give us access? | select (Meta Business Manager invite / LinkedIn page admin / Direct login via vault / I'll figure it out later) | No |

### Section S2 — Brand voice & content (7 questions)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `brand_voice_adjectives` | Brand voice — 3-5 adjectives | text | No |
| `known_for` | What you want to be known for | textarea | No |
| `content_pillars` | 3-5 content pillars (topics to dominate) | textarea | No |
| `off_limits_topics` | Topics that are OFF-LIMITS | textarea | No |
| `industry_sensitivities` | Industry restrictions on advertising or required disclosures | textarea | Yes |
| `accounts_love` | 2 brands or accounts whose social you love | textarea | Yes |
| `accounts_cringe` | 1 brand or account whose social makes you cringe | textarea | Yes |

### Section S3 — Operational policy (5 questions, ONLY shown for Standard + Full)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `posting_frequency_expected` | Posting frequency expected per channel per week | select (1-2 / 3-4 / 5-7) | No |
| `approval_workflow_social` | Approval workflow | select (Every post / Weekly batch / Monthly batch / Carte blanche) | No |
| `reply_policy` | Reply policy on public comments | select (All replies / Only positive / Only questions / Forward to you / Don't touch) — only for Standard+. Full management uses this as the default policy. | No |
| `dm_policy` | DM policy | same options as reply_policy | No |
| `crisis_protocol` | Crisis protocol — viral negative comment, bad review reply — who do we call within how many minutes? | textarea | No |

Basic-tier customers skip this section entirely. Total visible questions for Basic = lower, so progress reaches 100% genuinely.

### Section S4 — Brand assets (5 questions, SKIPPED if existing website client)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `logo_upload_social` | Logo upload (PNG or SVG) | file | No |
| `brand_colors_social` | Brand colors (hex codes, or "match my website") | text | No |
| `stock_photo_library` | Do you have headshots, action shots, work-in-progress shots we can use? | textarea | Yes |
| `photo_shoot_budget` | Can we hire a local photographer if needed? Budget per shoot? | text | Yes |
| `existing_graphics_templates` | Existing Canva account or brand guide? | text | Yes |

### Section S5 — Campaign calendar (3 questions)

| Key | Label | Type | Skip? |
|---|---|---|---|
| `upcoming_90_day_events` | Events, launches, or promotions in the next 90 days | textarea | Yes |
| `annual_recurring_dates` | Annual recurring dates (anniversaries, industry events, seasonal pushes) | textarea | Yes |
| `lead_magnets` | Specific offers or lead magnets we should promote | textarea | Yes |

### Section completion → SetupTodo creation

For each channel slot where `channel_<N>_access == 'Direct login via vault'`, create a SetupTodo with `credential_type` = the platform value from that slot. So if they said "Facebook" + "direct login via vault" → To-Do "Add Facebook login" appears.

---

## 10. Vault dropdown enhancement

### Concept
Add a structured `credential_type` field to vault credentials. Drives the To-Do auto-completion AND gives admin a sortable taxonomy.

### Schema change

```python
class VaultCredential(models.Model):
    # ... existing fields ...

    credential_category = models.CharField(max_length=20, choices=[
        ('social', 'Social profile'),
        ('cms', 'Website / CMS'),
        ('server', 'Server / hosting'),
        ('infra', 'Domain & infrastructure'),
        ('google', 'Google services'),
        ('other', 'Other'),
    ])
    credential_type = models.CharField(max_length=40)  # value from the type dropdown
    custom_label = models.CharField(max_length=100, blank=True)  # required if credential_type == 'other'
```

### Grouped dropdown UI

Two-step dropdown to avoid one massive list:

```
┌── Add credential ────────────────────────────────────────┐
│  Category                                                │
│    [Social profile ▾]                                    │
│                                                          │
│  Type                                                    │
│    [Facebook ▾]      ← populated from chosen category    │
│    (If "Other" → custom label text field appears)        │
│                                                          │
│  Custom label (only shown if Other)                      │
│    [Postmark account]                                    │
│                                                          │
│  Friendly name (optional)                                │
│    [Smith Family Law Facebook]                           │
│                                                          │
│  Username                                                │
│  Password                                                │
│  URL (optional)                                          │
│  Notes (optional)                                        │
└──────────────────────────────────────────────────────────┘
```

### Type taxonomy (grouped by category)

```
social: facebook, instagram, linkedin, twitter, tiktok, youtube,
        pinterest, threads, other

cms:    wordpress_admin, shopify_admin, squarespace_admin, wix_admin,
        webflow_admin, custom_site_admin, other

server: ssh, ftp_sftp, cpanel, hosting_panel, other

infra:  domain_registrar, cloudflare, email_workspace, other

google: google_analytics, google_search_console, google_business_profile,
        google_ads, other

other:  (anything not above — custom_label required)
```

### To-Do auto-completion logic

```python
def on_vault_credential_save(credential):
    matching_todo = SetupTodo.objects.filter(
        user=credential.user,
        status='pending',
        credential_type=credential.credential_type,
    ).first()
    if matching_todo:
        matching_todo.status = 'completed'
        matching_todo.completed_at = timezone.now()
        matching_todo.auto_completed_by = f'vault:{credential.id}'
        matching_todo.save()
```

### Admin edit path
Admin can edit a credential's `credential_type` from the admin vault view. If the new type matches an open SetupTodo for that user → auto-complete it. Same hook as the user-side save.

### "Other" + custom label — partial To-Do match

User concern: if customer selects "Other" + types "Facebook" as custom label, the To-Do won't auto-complete. Two options:

| Option | Pros | Cons |
|---|---|---|
| **Show all open To-Dos in vault sidebar with "Match" button** | User can manually link an "Other" credential to a To-Do | Adds UI complexity |
| **Just rely on the structured dropdown — show a hint when "Other" is selected: "If this is a Facebook login, pick 'Social profile → Facebook' instead so we can auto-track it on your To-Do list"** | Zero new UI | Trusts user to pick correctly |

**Recommendation: option 2** for v1. Simple hint, no manual-link UI. If we see customers consistently using "Other" wrong, add option 1 in v2.

### Backfill
All existing `VaultCredential` rows → set `credential_category='other'`, `credential_type='other'`, `custom_label=<existing name field value>`. Lossless. Admin can re-categorize as needed.

### Files to touch
- `vault/models.py` → add the three fields
- `vault/forms.py` → grouped dropdown
- `vault/templates/vault/credential_form.html` → category → type cascade
- `vault/views.py` → on-save hook
- `core/static/js/vault_credential_form.js` → cascading dropdown logic
- Migration

---

## 11. Setup To-Do widget

### Models

```python
class SetupTodo(models.Model):
    user                = models.ForeignKey(User, on_delete=models.CASCADE)
    task_type           = models.CharField(max_length=30, choices=[
        ('vault_credential', 'Vault credential'),
        ('google_access',    'Google service access'),
        ('manual',           'Manual task'),
    ])
    credential_type     = models.CharField(max_length=40, blank=True)
                          # matches VaultCredential.credential_type for auto-completion
    title               = models.CharField(max_length=120)
    description         = models.TextField(blank=True)
    deeplink_url        = models.CharField(max_length=255)
                          # e.g. /vault/add/?category=social&type=facebook
    status              = models.CharField(max_length=15, choices=[
        ('pending',   'Pending'),
        ('completed', 'Completed'),
    ], default='pending')
    completed_at        = models.DateTimeField(null=True, blank=True)
    auto_completed_by   = models.CharField(max_length=80, blank=True)
                          # e.g. "vault:42" or "manual:admin_zachery"
    reminder_3_sent     = models.BooleanField(default=False)
    reminder_7_sent     = models.BooleanField(default=False)
    reminder_14_sent    = models.BooleanField(default=False)
    created_at          = models.DateTimeField(auto_now_add=True)
```

### Sidebar widget

```
┌── Client portal sidebar ──────────┐
│  ▸ To-Do List (3)        ← top    │  ← count badge, opens modal
│  ─────────────────────────        │
│  Dashboard                        │
│  Projects                         │
│  ...                              │
└───────────────────────────────────┘
```

### Centered modal layout

```
┌── To-Do List ────────────────────────────────────── [×] ──┐
│                                                            │
│  Things we still need from you (3):                       │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ ☐ Add Facebook login                                 │ │
│  │   We need access to schedule your posts.             │ │
│  │   [Add credentials →]                                │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ ☐ Add Instagram login                                │ │
│  │   We need access to schedule your posts.             │ │
│  │   [Add credentials →]                                │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ ☐ Add WordPress admin login                          │ │
│  │   We need this to push updates + security patches.   │ │
│  │   [Add credentials →]                                │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
│  ▸ Completed (2)              ← collapsible accordion     │
│    ✓ Add Twitter login           (completed 2 hrs ago)    │
│    ✓ Add domain registrar login  (completed 1 day ago)    │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### Reminder email cadence

| Day | Trigger | Email content |
|---|---|---|
| 3 | Any pending SetupTodo created 3+ days ago, `reminder_3_sent=False` | List all open items, link to portal To-Do |
| 7 | Same, day 7, `reminder_7_sent=False` | "Friendly reminder — these are still open" |
| 14 | Same, day 14, `reminder_14_sent=False` | "Last reminder — please complete so we can do our job" |

Celery beat task `send_setup_todo_reminders` runs daily.

### Deep-link pattern
```
deeplink_url = /vault/add/?category=social&type=facebook
```
Vault add-credential form reads URL params on load and pre-selects the category + type dropdowns. User just fills in username + password + saves.

### Files to create
- `clients/models.py` → `SetupTodo`
- `clients/views.py` → `todo_modal` (HTMX partial for the modal body)
- `clients/templates/clients/_todo_modal.html`
- `clients/tasks.py` → `send_setup_todo_reminders`
- `AspiredWebsitesRevamped/settings.py` → beat schedule entry
- Migration
- Sidebar template update: add To-Do entry with badge

---

## 12. Subscription management UI

### Scope (all on aspiredwebsites.com — Stripe Customer Portal NOT used)

| Screen | Path |
|---|---|
| Manage Billing hub | `/portal/billing/` |
| Active subscriptions | `/portal/billing/subscriptions/` |
| Cancel subscription | `/portal/billing/subscriptions/<id>/cancel/` |
| Change plan (upgrade/downgrade) | `/portal/billing/subscriptions/<id>/change/` |
| Payment methods | `/portal/billing/cards/` |
| Add card | `/portal/billing/cards/add/` (Stripe Setup Intent + Elements) |
| Remove card | `/portal/billing/cards/<id>/remove/` |
| Set default card | `/portal/billing/cards/<id>/default/` |
| Past invoices | `/portal/billing/invoices/` |
| Download invoice PDF | `/portal/billing/invoices/<id>/pdf/` (proxy to Stripe-hosted PDF URL) |

### Cancel flow
```
1. [Cancel subscription] button on the subscription detail page
2. Confirm modal:
   "Cancel your {plan_name} subscription? It stays active until your
   next billing date ({date}). You'll lose access to {benefits}."
3. Optional: "Tell us why" textarea (saved on a CancellationReason model)
4. [Confirm cancel]
   - Stripe API: subscription.modify(id, cancel_at_period_end=True)
   - User sees: "Cancelled. Active through {date}."
5. Day-of-period-end: webhook customer.subscription.deleted → mark plan inactive
```

### Change plan flow
```
1. [Change plan] button on the subscription detail page
2. List of compatible plans (e.g., Maintenance Essentials shows Growth + Dominant)
3. Selection → show proration preview
   - Stripe API: invoice.upcoming(subscription=id, subscription_items=[...])
4. [Confirm change]
   - Stripe API: subscription.modify with new prices
5. Show new billing summary
```

### New models
```
class CancellationReason(models.Model):
    user             = models.ForeignKey(User, on_delete=models.CASCADE)
    subscription_id  = models.CharField(max_length=80)
    reason           = models.TextField()
    cancelled_at     = models.DateTimeField(auto_now_add=True)
```

### Files to create
- `billing/portal_views.py` → all the above views
- `billing/templates/billing/portal_*.html` (multiple)
- `core/static/js/billing_card_form.js` — Stripe Elements for new card
- `clients/templates/clients/portal_sidebar.html` — add "Manage Billing"

### Open questions
- Show grace period / dunning state in UI?
- Allow downgrade mid-period or only at renewal?
- Plan-change pro-ration — pass-through to customer or admin-approved?

---

## 13. Admin-side changes

### Lead detail page (`/admin-dashboard/leads/<id>/`)
- New section: "Opt-in add-ons" listing `opted_in_addons` with consume status
- New section: "Scheduled call" with link to Google Calendar event (if any)
- Stale-opt-in pill (after 90 days of admin inactivity)

### Invoice creation flow
When admin clicks "Send Invoice" on a Lead with opted-in add-ons:
- New form pre-populates:
  - Design package (line item)
  - Each opted-in add-on (line item)
  - 10%-first-month coupon pre-applied to opted-in add-on line items
  - All checkboxes pre-checked
- Admin can uncheck before sending

### Onboarding monitor (`/admin-dashboard/onboarding/`)
- New admin view: list of all in-progress Onboardings
- Per-row: customer, product_type, % complete, last activity, # of open SetupTodos
- Filter by product_type, completion status, stale (>14 days no activity)

### Schedule availability manager (`/admin-dashboard/schedule/`)
- List of `AvailabilityWindow` rows
- Add/edit/delete day-of-week + time-range entries
- Google Calendar connection status (OAuth)
- "Sync now" button

### Files to touch (existing)
- `admin_dashboard/views.py` → extend lead_detail context, add new views
- `admin_dashboard/templates/admin_dashboard/lead_detail.html` → add opt-in section
- `admin_dashboard/templates/admin_dashboard/onboarding_list.html` (new)
- `admin_dashboard/templates/admin_dashboard/availability.html` (new)
- `admin_dashboard/urls.py` → new routes

---

## 14. Google integrations — TODO (Phase 9, deferred)

Marked here so we don't forget. Onboarding routes "No" answers to a "We'll set it up for you" placeholder until these land.

### Google Business Profile auto-create
- Customer answers "No, I don't have a GBP"
- Background task uses Google My Business API to create a Location under your manager account
- Add customer's email as a manager (they accept via email invite from Google)
- Profile pre-populated from intake data (business name, address, hours)

### Google Analytics 4 auto-create
- Customer answers "No, I don't have GA"
- Background task uses Google Analytics Admin API to create a GA4 property
- Grant customer admin access to the property
- Provide them with the measurement ID + tag installation instructions
- Auto-install if they're on our hosting

### Google Search Console auto-verify
- Customer answers "No, I don't have GSC"
- Background task uses Google Search Console API to add their domain
- Verify via DNS TXT record (we add it on their behalf if domain is registered with us)
- Grant customer access

### Scope
~2 weeks per integration including OAuth flows, error handling, and customer-facing copy. Best to do them sequentially after the main onboarding refactor is stable.

---

## 15. Complete model inventory

### New models
| Model | Purpose | App |
|---|---|---|
| `Onboarding` | Per-user, per-product onboarding state | `clients` |
| `OnboardingResponse` | Each question's answer | `clients` |
| `SetupTodo` | Post-onboarding task tracker | `clients` |
| `PasswordSetupToken` | Magic-link tokens | `clients` |
| `AvailabilityWindow` | Admin's bookable hours | `clients` |
| `ScheduledCall` | Customer-booked calls | `clients` |
| `CancellationReason` | Why customers cancelled | `billing` |

### Modified models
| Model | Change | App |
|---|---|---|
| `User` | Add `password_setup_required` BooleanField | `clients` (or wherever User lives) |
| `VaultCredential` | Add `credential_category`, `credential_type`, `custom_label` | `vault` |
| `Lead` | Add `opted_in_addons` JSON, `opted_in_addons_at`, `scheduled_call_id` FK | `outreach` |
| `ServiceTier` | Add `max_channels` for social tiers, `reply_handling` (`none`/`triage`/`full`) | `billing` |

### Migration order
Each phase has its own migration. Run in phase order to avoid foreign-key cycles:
1. Phase 1: VaultCredential additions
2. Phase 2: Onboarding + OnboardingResponse + User.password_setup_required
3. Phase 3: SetupTodo
4. Phase 5: ServiceTier additions (max_channels, reply_handling)
5. Phase 6: PasswordSetupToken
6. Phase 7: CancellationReason
7. Phase 8: AvailabilityWindow + ScheduledCall + Lead additions

---

## 16. Open questions — ALL RESOLVED ✓

| # | Question | Decision |
|---|---|---|
| 1 | Posts-per-channel cadence | **8 / 12 / 16** (Basic / Standard / Full) |
| 2 | Default availability windows | **Mon–Fri 4pm–8pm ET, Sat 9am–8pm ET, Sun closed** |
| 3 | Stripe Address Element on checkout | **Required** |
| 4 | Stripe Tax | **Skip for now** (revisit if/when serving consumer or out-of-state at scale) |
| 5 | Plan change proration | **Pass through to customer** (Stripe-native) |
| 6 | Downgrade timing | **End of period** |
| 6b | Upgrade timing (added) | **Immediate, standard prorated charge** (Stripe's `proration_behavior='create_prorations'`) |
| 7 | "Other" vault credential manual To-Do link UI | **Defer to v2.** Show hint on "Other" selection: "If this is a Facebook login, pick Social profile → Facebook so we can auto-track it." |
| 8 | Refund policy | **Drafted** at `docs/refund_policy_draft.md`. Final approved by Zachery 2026-06-07. Publishes during Phase 5 alongside the pricing page redesign. |
| 9 | Welcome screen estimates | **12 / 18 / 15 / 22 / 30 minutes** (maintenance / maintenance+move-over / social basic / standard / full) |
| 10 | S3 (Reply/DM policy) | **Fully required for Standard + Full tiers — no Skip button on any question.** Section is hidden entirely for Basic (so doesn't apply). |

### Additional decisions captured in revision
- **Maintenance gets a first-month satisfaction guarantee** (refund first month if dissatisfied). Social does NOT — content takes 60-90 days to show results.
- **Web design refund language reworded** to emphasize "we fix it, refund is genuinely last resort" — fix-it commitment becomes the headline guarantee.
- **Jurisdiction is Fulton County, Georgia** (Atlanta) — Aspired Websites LLC is registered in GA, not TX. CLAUDE.md's mention of "Texas and Georgia clients" is the customer target market, not the company location.

---

## 17. What this build does NOT include (out of scope)

- Cart functionality (each Buy Now is a single-item checkout — no shared cart)
- Annual billing (everything is monthly subscription except hosting which is annual)
- Multi-currency (USD only)
- Multi-language (English only)
- Referral / affiliate codes (separate Phase 7 build already exists)
- Custom domains for the portal (everyone uses aspiredwebsites.com/portal/)
- White-labeling (your brand only)
- Multi-tenancy (one Aspired Websites instance)

---

## 18. Estimated build effort

Rough order-of-magnitude for full implementation (one developer, focused):

| Phase | Effort |
|---|---|
| 1. Vault enhancement | 1 day |
| 2. Onboarding wizard framework | 3-4 days |
| 3. Setup To-Do widget | 2 days |
| 4. Maintenance + Social onboarding flows | 2-3 days |
| 5. Pricing page + custom checkout | 5-7 days |
| 6. Account creation + magic-link | 1-2 days |
| 7. Subscription management UI | 4-5 days |
| 8. Schedule-a-Call calendar system | 4-6 days |

**Total: roughly 22-30 working days** for a full delivery. Each phase ships independently so we can pause/redirect between phases.

---

## 19. Approval gates

Before any phase begins, get explicit go-ahead. Recommended sequence:

1. **Review this spec doc end-to-end** — flag anything wrong
2. **Lock open questions in section 16** — answer all 10 before Phase 1 starts
3. **Phase 1 build** — review when done
4. **Phase 2 build** — review when done
5. ... etc.

No "ship Phase 1-8 in one go." Each phase = its own review + sign-off.

---

*End of spec.*
