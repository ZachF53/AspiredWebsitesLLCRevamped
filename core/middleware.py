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
        else:
            response['Content-Security-Policy'] = CSP_PUBLIC
        response['Permissions-Policy'] = PERMISSIONS_POLICY
        # Belt-and-suspenders: explicitly assert nosniff even though
        # Django's SecurityMiddleware also sets this.
        response.setdefault('X-Content-Type-Options', 'nosniff')
        return response
