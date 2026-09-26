"""
Core public-facing static-content views.

Privacy Policy + Terms of Service render under the public site
chrome. Effective date is rendered into the template context so a
single source of truth lives here in code, not in two prose
templates that would drift apart.
"""

from datetime import date

from django.shortcuts import render


LEGAL_EFFECTIVE_DATE = date(2026, 9, 25)
# The refund policy was revised on its own (first-month vs 30-day guarantee).
REFUND_EFFECTIVE_DATE = date(2026, 9, 26)


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
