"""
Core public-facing static-content views.

Privacy Policy + Terms of Service render under the public site
chrome. Effective date is rendered into the template context so a
single source of truth lives here in code, not in two prose
templates that would drift apart.
"""

from datetime import date

from django.shortcuts import render


LEGAL_EFFECTIVE_DATE = date(2026, 5, 23)


def privacy_policy(request):
    return render(request, 'core/privacy_policy.html', {
        'active_nav': '',
        'effective_date': LEGAL_EFFECTIVE_DATE,
        'meta_title': 'Privacy Policy — Aspired Websites',
        'meta_description': (
            'How Aspired Websites LLC collects, uses, and protects '
            'your personal information.'
        ),
    })


def terms_of_service(request):
    return render(request, 'core/terms.html', {
        'active_nav': '',
        'effective_date': LEGAL_EFFECTIVE_DATE,
        'meta_title': 'Terms of Service — Aspired Websites',
        'meta_description': (
            'Terms governing the use of aspiredwebsites.com and '
            'services from Aspired Websites LLC.'
        ),
    })
