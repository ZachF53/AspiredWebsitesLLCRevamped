"""
Security headers middleware for Aspired Websites.

Django's SecurityMiddleware already handles HSTS, X-Content-Type-Options,
SECURE_REFERRER_POLICY, and SECURE_CROSS_ORIGIN_OPENER_POLICY (set in
settings.py). XFrameOptionsMiddleware handles X-Frame-Options.

This middleware adds the two headers Django does not ship natively:
Content-Security-Policy and Permissions-Policy. CSP is relaxed for
/admin/ paths because Django admin uses inline styles and scripts.
"""

# Google Analytics 4 hosts, factored out because two policies need them.
#
# gtag.js is served from googletagmanager.com, then beacons out to the
# regional analytics endpoints (region1.google-analytics.com and friends),
# which is why the connect-src entries are wildcarded — GA picks the region
# at runtime and pinning one would silently drop hits from other geographies.
# The img-src entry covers gtag's fallback pixel on browsers where the
# fetch/sendBeacon path is unavailable.
#
# Deliberately NOT a blanket https: — these three names are the entire GA
# surface, and keeping the list explicit means an injected script still
# cannot exfiltrate to an arbitrary host.
GA_SCRIPT_SRC = 'https://www.googletagmanager.com'
GA_CONNECT_SRC = ('https://*.google-analytics.com '
                  'https://*.analytics.google.com '
                  'https://www.googletagmanager.com')
GA_IMG_SRC = 'https://*.google-analytics.com'

# Restrictive default CSP for the public site and client portal.
# - Scripts: 'self' plus googletagmanager.com (GA4 — see base.html).
# - Styles: 'self' only — no style="..." attributes in our templates.
# - Images: 'self' plus data: URIs (small inline SVGs/icons).
# - Forms post only to 'self'. No <iframe> framing allowed anywhere.
#
# The GA allowances widen this policy for the client portal too, which
# carries no GA tag. Accepted on purpose: one policy is far easier to
# reason about than a near-duplicate that differs by three hostnames, and
# permitting a host nothing loads from grants no capability by itself.
CSP_PUBLIC = (
    "default-src 'self'; "
    f"script-src 'self' {GA_SCRIPT_SRC}; "
    "style-src 'self'; "
    f"img-src 'self' data: {GA_IMG_SRC}; "
    "font-src 'self'; "
    f"connect-src 'self' {GA_CONNECT_SRC}; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

# Terminal CSP — the SSH terminal page. Scripts stay strict ('self' only; all
# terminal JS is external), but style-src allows inline because xterm.js
# applies dynamic styling at runtime. The page is staff-only and TOTP-gated.
CSP_TERMINAL = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

# Payment page CSP — public /pay/<token>/ page. Loads Stripe.js from
# js.stripe.com and the embedded Payment Element iframe runs on
# js.stripe.com. We also need to allow Stripe to phone home to
# api.stripe.com for the payment confirmation and 3DS redirects.
# Per spec the wallets are off, so Apple/Google/Link payment hooks are
# not enabled — but the Element still iframes a hooks subdomain for
# its own UI so we permit the broader stripe.com space.
# GA is allowed here too: the payment and contract pages extend base.html,
# so they carry the gtag — without these the tag is blocked on exactly the
# pages whose conversions matter most. img-src already permits https:.
CSP_PAYMENT = (
    "default-src 'self'; "
    f"script-src 'self' https://js.stripe.com {GA_SCRIPT_SRC}; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    f"connect-src 'self' https://api.stripe.com {GA_CONNECT_SRC}; "
    "frame-src https://js.stripe.com https://hooks.stripe.com; "
    "frame-ancestors 'none'; "
    "form-action 'self' https://js.stripe.com; "
    "base-uri 'self'; "
    "object-src 'none'"
)

# Recording-replay CSP — admin + portal session-replay pages. The rrweb
# Replayer mounts an iframe and reconstructs the captured client-site DOM
# inside it; that iframe inherits the parent CSP, so we must allow whatever
# the recorded page used:
#   - inline <style> blocks (rrweb's inlineStylesheet output)
#   - external stylesheets (Google Fonts, CDN-hosted CSS, etc.)
#   - client-origin images, blob: previews, and data: SVGs
#   - webfonts from any https origin (and data: URIs for inlined fonts)
# Scripts stay strict — the rrweb Replayer never executes captured <script>
# tags (they're reconstructed as inert DOM), so 'self' is sufficient. Both
# replay URLs are login-gated (staff or owning-client only).
CSP_REPLAY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https:; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https:; "
    "media-src 'self' blob: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

# Relaxed CSP for /admin/ — Django admin uses inline <style> and <script>.
CSP_ADMIN = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

# /admin-dashboard/ — CSP_PUBLIC with inline STYLES allowed.
#
# Why this exists: the admin dashboard was falling through to
# CSP_PUBLIC, whose `style-src 'self'` blocks the `style=` attribute.
# 266 inline styles across ~30 admin templates were being silently
# dropped, including every data-driven bar in the DMARC trend, the
# Redis monitor, the intelligence score bars, the conversion funnel
# and the leads table — which is why those charts rendered as empty
# boxes. Confirmed in the browser: an element with `style="height:
# 100%"` computed to 1px, with "Applying inline style violates the
# following Content Security Policy directive" in the console.
#
# The trade-off, stated plainly: `script-src` stays 'self'. That is
# the directive doing the real work against XSS, and it is untouched.
# What is relaxed is style-src, on a surface that is login-gated and
# staff-only (@admin_required). Rewriting 266 attributes into utility
# classes would be a large refactor that new code would quietly
# reintroduce anyway — the height of a bar is genuinely per-datum, and
# CSS cannot express it without either inline styles or a class per
# percentage point.
#
# Derived from CSP_PUBLIC by string replacement rather than retyped,
# so the two cannot drift apart when a directive changes.
CSP_ADMIN_DASHBOARD = CSP_PUBLIC.replace(
    "style-src 'self'; ", "style-src 'self' 'unsafe-inline'; ")

# Where browsers POST CSP violation reports (core.views.csp_report).
CSP_REPORT_PATH = '/csp-report/'

# Disable browser features we never use.
#
# Sept 2026 (plan M-6.08): ambient-light-sensor, battery, document-domain
# and web-share removed. Chrome doesn't recognise them in
# Permissions-Policy and logged an "Unrecognized feature" console warning
# on every page view; they were protecting nothing.
PERMISSIONS_POLICY = (
    "accelerometer=(), "
    "autoplay=(), "
    "camera=(), "
    "display-capture=(), "
    "encrypted-media=(), "
    "fullscreen=(self), "
    "geolocation=(), "
    "gyroscope=(), "
    "magnetometer=(), "
    "microphone=(), "
    "midi=(), "
    "payment=(), "
    "picture-in-picture=(), "
    "publickey-credentials-get=(), "
    "screen-wake-lock=(), "
    "sync-xhr=(), "
    "usb=(), "
    "xr-spatial-tracking=()"
)

# Back-office and token-gated routes: never indexed, even if a link to
# one leaks. Paired with the trimmed robots.txt (plan M-3.07), which no
# longer enumerates these paths.
NOINDEX_PREFIXES = (
    '/admin', '/portal/', '/onboarding/', '/pay/', '/plan-pay/',
    '/set-password/', '/maintenance/', '/sendgrid/', '/outreach/',
    '/ref/', '/proposals/', '/intelligence/', '/nps/', '/billing/',
    '/api/', '/login/', '/password-reset/', '/logout/',
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if getattr(response, 'keep_csp', False):
            # The view set its own, stricter policy (e.g. `sandbox` on a
            # private-document download) — don't replace it.
            pass
        elif path.startswith('/admin/'):
            response['Content-Security-Policy'] = CSP_ADMIN
        elif (path.startswith('/admin-dashboard/vault/')
              and path.endswith('/terminal/')):
            response['Content-Security-Policy'] = CSP_TERMINAL
        elif path.startswith((
            '/pay/',                        # public invoice payment page + success
            '/plan-pay/',                   # public plan payment page + success
            '/portal/subscriptions/',       # portal: add card via SetupIntent
            '/billing/checkout/',           # custom Stripe Elements checkout
            '/billing/portal/cards/add/',   # portal: add a new card
        )):
            # Every page that loads Stripe.js and renders the Stripe
            # Element iframe needs the Stripe-permissive policy. Without it
            # CSP_PUBLIC's `script-src 'self'` blocks js.stripe.com and the
            # card/address iframes never mount (and checkout.js bails before
            # wiring up the page). Keep this list in sync with any new page
            # that embeds Stripe Elements.
            response['Content-Security-Policy'] = CSP_PAYMENT
        elif '/recordings/' in path and path.endswith('/replay/'):
            # Matches both admin (/admin-dashboard/clients/<id>/recordings/
            # <rec>/replay/) and client portal (/portal/recordings/<rec>/
            # replay/) — relaxed so the rrweb replay iframe can render the
            # captured site's CSS, fonts, and images.
            response['Content-Security-Policy'] = CSP_REPLAY
        elif path.startswith('/admin-dashboard/'):
            # Last of the /admin-dashboard/ branches on purpose — the
            # vault terminal and the recording replay above are more
            # specific and must keep their own policies.
            response['Content-Security-Policy'] = CSP_ADMIN_DASHBOARD
        else:
            response['Content-Security-Policy'] = CSP_PUBLIC

        # Embedded admin tool pages (?embed=1) are lazy-loaded inside an
        # iframe by the Website detail Monitoring accordion. They must be
        # framable by the SAME origin only — relax frame-ancestors to
        # 'self' and downgrade X-Frame-Options from DENY to SAMEORIGIN.
        # No external site can frame them (no clickjacking surface).
        if (request.GET.get('embed') and path.startswith('/admin-dashboard/')
                and not getattr(response, 'keep_csp', False)):
            response['Content-Security-Policy'] = (
                response['Content-Security-Policy'].replace(
                    "frame-ancestors 'none'", "frame-ancestors 'self'"))
            response['X-Frame-Options'] = 'SAMEORIGIN'

        # Violation reporting for every policy set above (a view's own
        # keep_csp policy is left exactly as the view wrote it). report-uri
        # covers Firefox/Safari; report-to + Reporting-Endpoints covers
        # Chromium, which needs an absolute URL.
        if (not getattr(response, 'keep_csp', False)
                and 'Content-Security-Policy' in response):
            response['Content-Security-Policy'] += (
                f'; report-uri {CSP_REPORT_PATH}; report-to csp')
            response['Reporting-Endpoints'] = (
                f'csp="{request.build_absolute_uri(CSP_REPORT_PATH)}"')

        response['Permissions-Policy'] = PERMISSIONS_POLICY
        if path.startswith(NOINDEX_PREFIXES):
            response['X-Robots-Tag'] = 'noindex, nofollow'
        # Belt-and-suspenders: explicitly assert nosniff even though
        # Django's SecurityMiddleware also sets this.
        response.setdefault('X-Content-Type-Options', 'nosniff')
        return response


# ──────────────────────────────────────────────────────────────────────
# Staff "view as client" — the read-only boundary
# ──────────────────────────────────────────────────────────────────────

# Everything the client portal serves. /portal/ also covers /portal/domains/
# (mounted separately in the root urlconf, but under the same prefix).
#
# Deliberately NOT listed: /billing/checkout/, /pay/, /plan-pay/,
# /billing/webhook/ and /maintenance/. Those are token- or Stripe-gated
# flows that resolve their subject from a URL token rather than from the
# session, so an impersonation session has no bearing on them and
# blocking them here would only break real clients and real webhooks.
IMPERSONATION_PORTAL_PREFIXES = ('/portal/', '/billing/portal/')

# Hard-blocked for ALL methods, not just writes.
#
# /portal/credentials/ renders the client's stored site passwords in
# plaintext (clients/templates/clients/_pcred_card.html), and its PIN form
# writes failed-attempt and lockout columns on the Account — so a staff
# member fumbling the client's PIN would lock the actual client out of
# their own credentials. Neither belongs in a "look at what they see"
# session; the same secrets are available to staff through the admin vault.
IMPERSONATION_BLOCKED_PREFIXES = ('/portal/credentials',)

# Blocked on GET too, because these mutate on GET by design.
#
# intelligence_approve / intelligence_decline record a client's answer to
# a paid recommendation and email the admin, and they accept GET on
# purpose so the link in the recommendation email works without a form.
# That makes them unsafe to merely render: loading the page IS the
# action, so the non-GET rule below cannot catch them.
#
# They are also mounted at the ROOT urlconf (/intelligence/respond/...),
# outside IMPERSONATION_PORTAL_PREFIXES, which is the other reason they
# need naming explicitly. Matched by prefix because both carry a UUID.
IMPERSONATION_GET_DENY_PREFIXES = ('/intelligence/respond/',)

IMPERSONATION_SAFE_METHODS = frozenset(['GET', 'HEAD', 'OPTIONS'])


class ImpersonationGuardMiddleware:
    """Enforces that a staff "view as client" session cannot change data.

    The portal's action buttons are also disabled in the browser
    (portal_readonly.js), but that is cosmetic — it keeps the admin from
    clicking something by reflex. THIS is the actual boundary. The
    operator is still authenticated as themselves with a valid session
    and a valid CSRF token, so a re-enabled button would otherwise submit
    successfully.

    Runs after AuthenticationMiddleware: it needs both request.user and
    the session to decide anything.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from clients.impersonation import active_target, note_blocked

        # Cheapest possible exit for ordinary traffic: one session dict
        # lookup, no DB query, before anything else happens.
        if not request.session.get('impersonate_account_id'):
            request.impersonation_target = None
            request.is_read_only = False
            return self.get_response(request)

        # Revalidates staff status + operator identity on every request
        # and clears the session if either fails, so this returning None
        # means "not impersonating" and the request proceeds as the
        # logged-in user's own.
        target = active_target(request)
        request.impersonation_target = target
        request.is_read_only = target is not None
        if target is None:
            return self.get_response(request)

        path = request.path
        if path.startswith(IMPERSONATION_BLOCKED_PREFIXES):
            note_blocked(request)
            return self._refuse(
                request,
                'Client credentials are not viewable in view-as mode. '
                'Use the admin vault instead.')

        if path.startswith(IMPERSONATION_GET_DENY_PREFIXES):
            note_blocked(request)
            return self._refuse(
                request,
                'That link records a client decision, so it is blocked in '
                'view-as mode.')

        if (request.method not in IMPERSONATION_SAFE_METHODS
                and path.startswith(IMPERSONATION_PORTAL_PREFIXES)
                and not self._is_exempt(path)):
            note_blocked(request)
            return self._refuse(
                request,
                'Blocked — this is a read-only view of the client portal.')

        return self.get_response(request)

    @staticmethod
    def _is_exempt(path):
        """Routes that must keep working while impersonating.

        Exiting is itself a POST, so without this exemption the operator
        would be trapped in the client's portal until the session cookie
        expired — the block would have eaten the only way out.

        Logout needs no entry: it lives at /logout/ (public/urls.py),
        outside the portal prefixes, so it never reaches this check.
        """
        return path == '/portal/exit-view-as/'

    @staticmethod
    def _refuse(request, message):
        from django.http import HttpResponse

        # HTMX swaps the response body into the page, so a bare status
        # code renders as an empty region with no explanation. Give it
        # something to show.
        if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(
                f'<div class="portal-readonly-block">{message}</div>',
                status=403)
        return HttpResponse(message, status=403, content_type='text/plain')
