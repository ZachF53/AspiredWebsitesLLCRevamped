"""
Security headers middleware for Aspired Websites.

Django's SecurityMiddleware already handles HSTS, X-Content-Type-Options,
SECURE_REFERRER_POLICY, and SECURE_CROSS_ORIGIN_OPENER_POLICY (set in
settings.py). XFrameOptionsMiddleware handles X-Frame-Options.

This middleware adds the two headers Django does not ship natively:
Content-Security-Policy and Permissions-Policy. CSP is relaxed for
/admin/ paths because Django admin uses inline styles and scripts.
"""

# Restrictive default CSP for the public site and client portal.
# - Scripts: 'self' plus unpkg.com (HTMX is loaded from there).
# - Styles: 'self' only — no style="..." attributes in our templates.
# - Images: 'self' plus data: URIs (small inline SVGs/icons).
# - Forms post only to 'self'. No <iframe> framing allowed anywhere.
CSP_PUBLIC = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
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
CSP_PAYMENT = (
    "default-src 'self'; "
    "script-src 'self' https://js.stripe.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://api.stripe.com; "
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
        elif path.startswith('/pay/'):
            # Public payment page + success page. Allows Stripe.js,
            # the Stripe Element iframe, and api.stripe.com calls.
            response['Content-Security-Policy'] = CSP_PAYMENT
        elif path.startswith('/portal/subscriptions/'):
            # Portal subscriptions page — Stripe Elements for adding
            # new cards via SetupIntent. Same Stripe permissions as
            # the public pay page.
            response['Content-Security-Policy'] = CSP_PAYMENT
        elif '/recordings/' in path and path.endswith('/replay/'):
            # Matches both admin (/admin-dashboard/clients/<id>/recordings/
            # <rec>/replay/) and client portal (/portal/recordings/<rec>/
            # replay/) — relaxed so the rrweb replay iframe can render the
            # captured site's CSS, fonts, and images.
            response['Content-Security-Policy'] = CSP_REPLAY
        else:
            response['Content-Security-Policy'] = CSP_PUBLIC
        response['Permissions-Policy'] = PERMISSIONS_POLICY
        # Belt-and-suspenders: explicitly assert nosniff even though
        # Django's SecurityMiddleware also sets this.
        response.setdefault('X-Content-Type-Options', 'nosniff')
        return response
