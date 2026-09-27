"""
Core public-facing static-content views.

Privacy Policy + Terms of Service render under the public site
chrome. Effective date is rendered into the template context so a
single source of truth lives here in code, not in two prose
templates that would drift apart.
"""

import json
import logging
from datetime import date

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit


LEGAL_EFFECTIVE_DATE = date(2026, 9, 25)
# The refund policy was revised on its own (first-month vs 30-day guarantee).
REFUND_EFFECTIVE_DATE = date(2026, 9, 26)


csp_logger = logging.getLogger('security.csp')


def privacy_policy(request):
    return render(request, 'core/privacy_policy.html', {
        'active_nav': '',
        'effective_date': LEGAL_EFFECTIVE_DATE,
        'meta_title': 'Privacy Policy | Aspired Websites',
        'meta_description': (
            'How Aspired Websites LLC collects, uses, and protects '
            'your personal information.'
        ),
    })


def terms_of_service(request):
    return render(request, 'core/terms.html', {
        'active_nav': '',
        'effective_date': LEGAL_EFFECTIVE_DATE,
        'meta_title': 'Terms of Service | Aspired Websites',
        'meta_description': (
            'Terms governing the use of aspiredwebsites.com and '
            'services from Aspired Websites LLC.'
        ),
    })


def refund_policy(request):
    # The hourly rate is quoted in the policy; read it from the database
    # like every other price (CLAUDE.md: never hardcode prices).
    from billing.pricing_models import AddonPricing
    hourly = AddonPricing.objects.filter(
        slug='addon-hourly', is_active=True).first()
    return render(request, 'core/refund_policy.html', {
        'active_nav': '',
        'effective_date': REFUND_EFFECTIVE_DATE,
        'hourly_display': hourly.get_price_display() if hourly else 'Billed hourly',
        'meta_title': 'Refund Policy | Aspired Websites',
        'meta_description': (
            'Refund terms for website builds, the Full Plan, hosting, '
            'and add-on services from Aspired Websites LLC.'
        ),
    })


def csrf_failure(request, reason=''):
    """Branded CSRF-failure page. Wired via settings.CSRF_FAILURE_VIEW
    so a stale-token POST renders the same look as the rest of the
    site instead of the default Django page."""
    from django.shortcuts import render as _render
    return _render(request, '403_csrf.html', status=403)


# ── CSP violation reports ─────────────────────────────────────────────
# Browsers POST here (core.middleware adds report-uri / report-to). The
# endpoint is anonymous and cross-origin by nature, so it takes nothing
# on trust: POST only, a hard body cap, per-IP rate limit, and it only
# ever writes one truncated log line. Reports caused by browser
# extensions are dropped; they are noise, not attacks on this site.
_CSP_MAX_BODY = 16 * 1024
_CSP_IGNORED_SOURCES = ('chrome-extension', 'moz-extension',
                        'safari-extension', 'safari-web-extension')


def _csp_reports(payload):
    """Normalise both report formats to a list of plain dicts."""
    if isinstance(payload, dict) and 'csp-report' in payload:
        return [payload['csp-report']]               # report-uri format
    if isinstance(payload, list):                     # Reporting API format
        return [r.get('body', {}) for r in payload
                if isinstance(r, dict) and r.get('type') == 'csp-violation']
    return []


@csrf_exempt
@require_POST
@ratelimit(key='ip', rate='60/h', method='POST', block=False)
def csp_report(request):
    if getattr(request, 'limited', False):
        return HttpResponse(status=204)
    if len(request.body) > _CSP_MAX_BODY:
        return HttpResponse(status=413)
    try:
        payload = json.loads(request.body or b'null')
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    for report in _csp_reports(payload):
        if not isinstance(report, dict):
            continue
        blocked = str(report.get('blocked-uri') or report.get('blockedURL') or '')
        source = str(report.get('source-file') or report.get('sourceFile') or '')
        if blocked.startswith(_CSP_IGNORED_SOURCES) or source.startswith(_CSP_IGNORED_SOURCES):
            continue
        csp_logger.warning(
            'CSP violation: directive=%s blocked=%s page=%s source=%s',
            str(report.get('violated-directive')
                or report.get('effectiveDirective') or '')[:100],
            blocked[:200],
            str(report.get('document-uri') or report.get('documentURL') or '')[:200],
            source[:200],
        )
    return HttpResponse(status=204)
