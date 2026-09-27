"""
/llms.txt — a plain-markdown map of the site for AI assistants
(format: https://llmstxt.org).

Built per request from the same sources the pages use (ServiceTier and
AddonPricing for prices, Article and CaseStudy for content, site_facts for
location, phone, timeline and guarantee), so it can never quote a retired
price or link a removed article. Links are always absolute to the
production host: staging must not advertise itself as the canonical site.
"""
from decimal import Decimal

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from core.site_facts import (
    BUILD_TIMELINE, CALL_DURATION_MINUTES, GUARANTEE_DAYS,
    GUARANTEE_REFUND_PERCENT, GUARANTEE_RETAINED_PERCENT, LOCATION_STATEMENT,
    PHONE_DISPLAY, PHONE_NOTE,
)


def _money(value):
    value = Decimal(value)
    if value == value.to_integral_value():
        return f'${value:,.0f}'
    return f'${value:,.2f}'


def _base():
    host = getattr(settings, 'PRODUCTION_HOST', 'aspiredwebsites.com')
    return f'https://{host}'


def _link(title, path, note=''):
    line = f'- [{title}]({_base()}{path})'
    return f'{line}: {note}' if note else line


def _pricing_lines():
    from billing.pricing_models import AddonPricing, ServiceTier

    tiers = {
        t.slug: t for t in ServiceTier.objects.filter(
            slug__in=['hvac-build-full', 'hvac-build-installment',
                      'hvac-full-plan', 'hvac-plan-paid-in-full',
                      'hvac-hosting-security'],
            is_active=True, is_public=True)
    }
    addons = {
        a.slug: a for a in AddonPricing.objects.filter(
            slug__in=['addon-hourly', 'addon-location'], is_active=True)
    }
    lines = []
    full = tiers.get('hvac-build-full')
    inst = tiers.get('hvac-build-installment')
    if full and inst:
        lines.append(
            f'- Website build: {_money(full.price)} paid in full at signing, '
            f'or {_money(inst.price)}/month for 24 months '
            f'({_money(inst.price * 24)} total), first payment at signing.')
    elif full:
        lines.append(f'- Website build: {_money(full.price)} paid in full at signing.')
    fp = tiers.get('hvac-full-plan')
    ppf = tiers.get('hvac-plan-paid-in-full')
    if fp and ppf:
        lines.append(
            f'- Full Plan (build financed): {_money(fp.price)}/month for 24 '
            f'months, then {_money(ppf.price)}/month. Includes hosting, '
            'maintenance, unlimited content updates, security patching, a '
            'monthly security report and automated review generation.')
        lines.append(
            f'- Full Plan (build already paid): {_money(ppf.price)}/month.')
    host = tiers.get('hvac-hosting-security')
    if host:
        lines.append(
            f'- Hosting + Security only: {_money(host.price)}/month, billed '
            'monthly. Content edits are billed hourly; no review automation.')
    hourly = addons.get('addon-hourly')
    if hourly:
        lines.append(
            f'- Out-of-scope work: {_money(hourly.price_min)}/hour, quoted and '
            'approved before work starts.')
    location = addons.get('addon-location')
    if location:
        lines.append(
            f'- Additional locations: from {_money(location.price_min)} per '
            'location, quoted in writing before signing.')
    return lines


def build_llms_txt():
    from clients.models import CaseStudy
    from public.models import Article

    out = [
        '# Aspired Websites',
        '',
        '> Aspired Websites LLC builds custom-coded websites with automated '
        'Google review generation for HVAC contractors, and for plumbing, '
        'electrical and other home-service trades at the same price. '
        f'{LOCATION_STATEMENT}',
        '',
        'Key facts:',
        '',
        '- Hand-coded sites (no WordPress, no page builders), built '
        f'mobile-first. A build takes {BUILD_TIMELINE}.',
        '- Built by Zachery Long, CISSP, M.S. Cybersecurity and Information '
        'Assurance. Aspired Websites is one engineer; clients talk directly '
        'to the person who builds the site.',
        '- Clients own their domain from day one and own the site and code '
        'once the build is paid in full. Plans are month-to-month with 30 '
        'days\' notice; no annual contracts.',
        f'- {GUARANTEE_DAYS}-day guarantee: cancel within {GUARANTEE_DAYS} '
        f'days of signing for a {GUARANTEE_REFUND_PERCENT}% refund of '
        f'everything paid ({GUARANTEE_RETAINED_PERCENT}% retained for work '
        'done).',
        '- Review automation asks every customer the same way after every '
        'completed job, triggered from the client\'s job software by webhook '
        'or Zapier. It never filters for happy customers or offers '
        'incentives, in line with Google\'s review policies.',
        f'- Phone: {PHONE_DISPLAY} ({PHONE_NOTE}). Email: '
        'zacherylong@aspiredwebsites.com.',
        f'- Next step: a free {CALL_DURATION_MINUTES}-minute strategy call, '
        f'booked at {_base()}{reverse("scheduler:design_schedule")}',
        '',
    ]

    pricing = _pricing_lines()
    if pricing:
        out += ['## Pricing', '']
        out += pricing
        out += [
            '',
            f'Full pricing, total cost over 24 and 36 months, and the fine '
            f'print: {_base()}{reverse("public:pricing")}',
            '',
        ]

    out += [
        '## Services',
        '',
        _link('Custom HVAC website', reverse('public:service_web_design'),
              'what a build includes, the week-by-week process, and '
              'measurement'),
        _link('Automated review generation',
              reverse('public:service_review_automation'),
              'how the request is triggered, timing, opt-out, and what it '
              'will not do'),
        _link('Hosting & maintenance',
              reverse('public:service_hosting_maintenance'),
              'dedicated server per client, patching, SSL, uptime, and the '
              'monthly security report'),
        _link('Pricing', reverse('public:pricing'),
              'every price, the 30-day guarantee, ownership terms and FAQ'),
        '',
    ]

    studies = CaseStudy.objects.filter(is_published=True).exclude(
        slug='').exclude(slug=None).order_by('-published_at')
    if studies:
        out += ['## Portfolio', '']
        for study in studies:
            out.append(_link(study.title, study.get_absolute_url(),
                             (study.summary or '').strip()))
        out.append('')

    articles = Article.objects.filter(
        status='published', is_archived=False).order_by('-published_at')
    if articles:
        out += ['## Insights', '']
        for article in articles:
            out.append(_link(article.title, article.get_absolute_url(),
                             article.summary.strip()))
        out.append('')

    out += [
        '## Company',
        '',
        _link('About', reverse('public:about'),
              'the founder, credentials and how to verify them'),
        _link('Contact', reverse('public:contact')),
        _link('Book a strategy call', reverse('scheduler:design_schedule')),
        _link('Free website audit', reverse('public:audit'),
              'an instant speed and security check; no email required'),
        '',
        '## Optional',
        '',
        _link('Warner Robins', reverse('public:location_warner_robins')),
        _link('Atlanta', reverse('public:location_atlanta')),
        _link('San Antonio', reverse('public:location_san_antonio')),
        _link('Sample monthly security report',
              reverse('public:sample_security_report')),
        _link('Terms of service', reverse('core:terms_of_service')),
        _link('Refund policy', reverse('core:refund_policy')),
        _link('Privacy policy', reverse('core:privacy_policy')),
        '',
    ]
    return '\n'.join(out)


@require_GET
@cache_control(public=True, max_age=3600)
def llms_txt(request):
    return HttpResponse(build_llms_txt(),
                        content_type='text/plain; charset=utf-8')
