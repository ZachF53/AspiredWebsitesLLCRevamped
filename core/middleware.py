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

# Disable browser features we never use.
PERMISSIONS_POLICY = (
    "accelerometer=(), "
    "ambient-light-sensor=(), "
    "autoplay=(), "
    "battery=(), "
    "camera=(), "
    "display-capture=(), "
    "document-domain=(), "
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
    "web-share=(), "
    "xr-spatial-tracking=()"
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if path.startswith('/admin/'):
            response['Content-Security-Policy'] = CSP_ADMIN
        elif (path.startswith('/admin-dashboard/vault/')
              and path.endswith('/terminal/')):
            response['Content-Security-Policy'] = CSP_TERMINAL
        elif path.startswith((
            '/pay/',                        # public invoice payment page + success
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
        if request.GET.get('embed') and path.startswith('/admin-dashboard/'):
            response['Content-Security-Policy'] = (
                response['Content-Security-Policy'].replace(
                    "frame-ancestors 'none'", "frame-ancestors 'self'"))
            response['X-Frame-Options'] = 'SAMEORIGIN'

        response['Permissions-Policy'] = PERMISSIONS_POLICY
        # Belt-and-suspenders: explicitly assert nosniff even though
        # Django's SecurityMiddleware also sets this.
        response.setdefault('X-Content-Type-Options', 'nosniff')
        return response
