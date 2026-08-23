import logging
import re
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.mail import send_mail
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from core.analytics import queue_event
from core.site_facts import LOCATION_PHRASE, LOCATION_STATEMENT

from .forms import AuditEmailForm, AuditForm, ContactForm
from .models import AuditLead

logger = logging.getLogger(__name__)


def _domain_of(url):
    """
    Bare hostname for an analytics param — 'example.com', never the
    full URL. A path can carry a query string, and a query string is
    exactly where a stray identifier would end up in GA4 (§5.3).
    """
    try:
        host = urlparse(url if '://' in url else f'http://{url}').hostname
    except ValueError:
        return ''
    return (host or '').lower().removeprefix('www.')


PAGESPEED_API_URL = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
PAGESPEED_TIMEOUT_SECONDS = 45

# Maps our score-dict keys to the audit service's category keys.
_CATEGORY_KEYS = {
    'performance':    'performance',
    'seo':            'seo',
    'best_practices': 'best-practices',
    'accessibility':  'accessibility',
}

_CATEGORY_LABELS = {
    'performance':    'Performance',
    'seo':            'SEO',
    'best_practices': 'Best Practices',
    'accessibility':  'Accessibility',
}

_TIER_LABELS = {
    'strong':     'Strong',
    'needs-work': 'Needs Work',
    'critical':   'Critical',
}

# Plain-English impact statement shown on every result card, keyed by
# category then score tier.
_IMPACT_STATEMENTS = {
    'performance': {
        'strong':     'Fast load times keep visitors on your site and signal '
                      'quality to Google.',
        'needs-work': 'Slow load times are costing you visitors. Most people '
                      'leave if a site takes more than 3 seconds to load.',
        'critical':   'Your site is critically slow. Visitors are leaving '
                      'before they even see your content — and Google is '
                      'penalizing your ranking.',
    },
    'seo': {
        'strong':     'Your site is well-optimized for search engines. Google '
                      'can find and rank your pages effectively.',
        'needs-work': 'Your SEO has gaps that are limiting how often you show '
                      'up in search results.',
        'critical':   'Critical SEO issues mean Google struggles to understand '
                      'and rank your site. You are likely invisible in search.',
    },
    'best_practices': {
        'strong':     'Your site follows web standards and security best '
                      'practices — a good foundation.',
        'needs-work': 'Your site has technical issues that affect security '
                      'and user trust.',
        'critical':   'Serious technical and security issues detected. These '
                      'affect both user trust and search rankings.',
    },
    'accessibility': {
        'strong':     'Your site is accessible to all users including those '
                      'using assistive technology.',
        'needs-work': 'Some users may have difficulty using your site — this '
                      'also affects SEO.',
        'critical':   'Major accessibility barriers detected. A significant '
                      'portion of visitors cannot fully use your site.',
    },
}


def _score_tier(score):
    """Map a 0-100 score to its tier: strong / needs-work / critical."""
    if score >= 90:
        return 'strong'
    if score >= 50:
        return 'needs-work'
    return 'critical'


def home(request):
    # The "Recent Builds" strip was four hardcoded cards with
    # placeholder visuals and copy that had drifted from the database —
    # it described Denis Law Group as a "personal-injury practice" when
    # the firm actually does family law and adoption. Driving it from
    # the same CaseStudy rows as /portfolio/ fixes the wrong detail,
    # brings in the real screenshots, and means it cannot drift again.
    from clients.models import CaseStudy
    return render(request, 'public/home.html', {
        'active_nav': 'home',
        'studies': CaseStudy.objects.filter(
            is_published=True).order_by('-published_at')[:4],
        'meta_title': 'Custom Websites for Law Firms and Small Businesses',
        'meta_description': (
            'Aspired Websites builds hand-coded, security-hardened websites '
            'for law firms and small businesses in Texas and Georgia. Led by '
            'a CISSP-certified cybersecurity engineer.'
        ),
    })


def _coming_soon(request, *, title, blurb, active_nav):
    return render(request, 'public/_placeholder.html', {
        'page_title': title,
        'blurb': blurb,
        'active_nav': active_nav,
        'meta_title': title,
    })


def law_firms(request):
    from billing.pricing_models import ServiceTier

    def _price_range(category):
        tiers = list(ServiceTier.get_active(category).order_by('price'))
        if not tiers:
            return ''
        low, high = tiers[0].price, tiers[-1].price
        if low == high:
            return f'${low:,.0f}'
        return f'${low:,.0f}–${high:,.0f}'

    # Legal work only — this section is headed "Sites We've Built for
    # Legal", so a non-legal client appearing under it would be a false
    # claim. Was a hardcoded Denis Law Group card with a gradient
    # placeholder; now it carries the real screenshot and cannot drift
    # from the case study it links to.
    from clients.models import CaseStudy
    return render(request, 'public/law_firms.html', {
        'active_nav': 'law_firms',
        'legal_studies': CaseStudy.objects.filter(
            is_published=True, business_type__icontains='law'
        ).order_by('-published_at'),
        'meta_title': 'Custom Websites for Law Firms',
        'meta_description': (
            'Hand-coded, security-hardened websites built specifically '
            'for law firms. CISSP-certified, built so your required bar '
            'disclaimers are easy to maintain. No FindLaw lock-in.'
        ),
        'build_range': _price_range('website_build'),
        'maintenance_range': _price_range('maintenance'),
    })


def portfolio(request):
    """
    Portfolio index — now driven by published CaseStudy rows.

    Previously four hardcoded cards. Master Plan §11 requires each
    project to have its own indexable URL, which needs them to be data
    rather than markup. Seeded by `manage.py seed_case_studies`.
    """
    from clients.models import CaseStudy
    return render(request, 'public/portfolio.html', {
        'active_nav': 'portfolio',
        'case_studies': CaseStudy.objects.filter(
            is_published=True).order_by('-published_at', '-created_at'),
        'meta_title': 'Portfolio — Aspired Websites',
        'meta_description': (
            'Recent work by Aspired Websites: Denis Law Group, '
            'Food Trucks of San Antonio, Moonieful Designs, and '
            'Burgland Technologies. Hand-coded, mobile-first.'
        ),
    })


def case_study_detail(request, slug):
    """
    /portfolio/<slug>/ — one indexable page per project (§11).

    Only published studies are reachable; an unpublished one 404s
    rather than 403s, so an unannounced project stays genuinely
    invisible.
    """
    from django.shortcuts import get_object_or_404
    from clients.models import CaseStudy
    study = get_object_or_404(CaseStudy, slug=slug, is_published=True)
    return render(request, 'public/case_study_detail.html', {
        'active_nav': 'portfolio',
        'study': study,
        'breadcrumbs': [
            ('Portfolio', '/portfolio/'),
            (study.title, None),
        ],
    })


def service_web_design(request):
    return render(request, 'public/service_web_design.html', {
        'active_nav': 'services',
        'active_service': 'web_design',
        'breadcrumbs': [
            ('Services', '/services/web-design/'),
            ('Custom Web Design', None),
        ],
    })


def service_digital_marketing(request):
    # Social plans render from the same active ServiceTier rows the
    # pricing page uses. The template used to hardcode its own names,
    # prices and channel counts, and they had drifted from the database.
    from billing.pricing_models import ServiceTier

    social = ServiceTier.get_active('social_media')
    # The structured-data priceRange was the last hardcoded price on this
    # page ("$399 - $999 / month"). Schema markup is still a public price
    # claim, so it comes from the same rows as the cards.
    prices = sorted(tier.price for tier in social)
    price_range = (
        f'${prices[0]:,.0f} - ${prices[-1]:,.0f} / month' if prices else '')

    return render(request, 'public/service_digital_marketing.html', {
        'active_nav': 'services',
        'active_service': 'digital_marketing',
        'social': social,
        'social_price_range': price_range,
        'breadcrumbs': [
            ('Services', '/services/web-design/'),
            ('Digital Marketing', None),
        ],
    })


def service_seo(request):
    return render(request, 'public/service_seo.html', {
        'active_nav': 'services',
        'active_service': 'seo',
        'breadcrumbs': [
            ('Services', '/services/web-design/'),
            ('SEO', None),
        ],
    })


def service_custom_web_development(request):
    """
    /services/web-design/custom-web-development/ — ~3,780/mo across
    `custom website design` (1,900), `custom web development` (1,000)
    and `custom web design` (880).

    Leads with "custom", never "hand coded" — that phrase is the brand
    story but only 10 searches/mo, so it belongs in the body copy
    rather than the H1 (KEYWORD_RESEARCH_FINDINGS.md §2.1).

    §6.1: the homepage owns generic `web design` / `web design
    company`; this page owns the "custom" qualifier specifically.
    """
    return render(
        request, 'public/service_custom_web_development.html', {
            'active_nav': 'services',
            'active_service': 'custom_web_development',
            'breadcrumbs': [
                ('Services', '/services/web-design/'),
                ('Web Design', '/services/web-design/'),
                ('Custom Web Development', None),
            ],
        })


def location_san_antonio(request):
    """
    /locations/san-antonio/ — the one location page (D5).

    2,860/mo explicit demand, 14x the build threshold, and the only
    city the homepage cannot credibly serve because the homepage is
    Atlanta-branded.

    Served 100% remotely — there is no San Antonio office and the page
    says so outright (§15: no fake offices). What makes it substantive
    rather than a thin city page is that three of our four case
    studies are genuinely San Antonio clients.
    """
    from clients.models import CaseStudy
    return render(request, 'public/location_san_antonio.html', {
        'active_nav': '',
        'sa_studies': CaseStudy.objects.filter(
            is_published=True, location__icontains='San Antonio'
        ).order_by('-published_at'),
        'breadcrumbs': [
            ('San Antonio Web Design', None),
        ],
    })


def _published_studies(limit=4):
    """Published case studies, newest first — shared by the location pages."""
    from clients.models import CaseStudy
    return CaseStudy.objects.filter(
        is_published=True).order_by('-published_at')[:limit]


def location_atlanta(request):
    """
    /locations/atlanta/ — revised D5, Aug 2026.

    D5 originally said no Atlanta page because "the homepage already
    owns Atlanta intent (title/H1/schema)". Two thirds of that is gone:
    the Atlanta address was a registered-agent suite and was removed
    from the footer and schema, and the homepage title has since been
    retargeted to the service. 2,160/mo of explicit demand was left
    with nothing pointed at it.

    §15 bites harder here than on San Antonio, because there are no
    Atlanta case studies to lean on. What the page has instead is true:
    Aspired is a Georgia company ~100 miles down I-75, so "we can be
    there" is a fact rather than a claim. `has_atlanta_study` drives an
    explicit "no Atlanta clients yet" note — the page states the gap
    rather than implying local work that does not exist, and it will
    stop saying so on its own once an Atlanta client is published.
    """
    from clients.models import CaseStudy
    return render(request, 'public/location_atlanta.html', {
        'active_nav': '',
        'studies': _published_studies(),
        'has_atlanta_study': CaseStudy.objects.filter(
            is_published=True, location__icontains='Atlanta').exists(),
        'breadcrumbs': [
            ('Atlanta Web Design', None),
        ],
    })


def location_warner_robins(request):
    """
    /locations/warner-robins/ — revised D5, Aug 2026.

    Rejected originally on volume (~10/mo explicit, under the 50/mo
    floor) and on volume alone that was correct. Built anyway because
    it is the only page on the site where "we are local to you" is
    literally true: the ProfessionalService schema and footer NAP both
    resolve to Warner Robins.

    That makes it the anchor the Google Business Profile work needs — a
    service-area GBP wants a crawlable page whose city matches the
    profile and the site's schema. It is a credibility and local-signal
    asset, not a traffic play, and should not be judged on sessions.
    """
    from clients.models import CaseStudy
    local = Q(location__icontains='Warner Robins') | Q(
        location__icontains='Macon') | Q(location__icontains='GA')
    return render(request, 'public/location_warner_robins.html', {
        'active_nav': '',
        'studies': _published_studies(),
        'has_local_study': CaseStudy.objects.filter(
            local, is_published=True).exists(),
        'breadcrumbs': [
            ('Warner Robins Web Design', None),
        ],
    })


def insights_index(request):
    """
    /insights/ — the blog index (Master Plan §12).

    Only published articles. Draft posts are invisible rather than
    login-gated: an unfinished article should not exist publicly at
    all, in any form.
    """
    from .models import Article
    return render(request, 'public/insights_index.html', {
        'active_nav': 'insights',
        'articles': Article.objects.filter(status='published'),
        'meta_title': 'Insights',
        'meta_description': (
            'Straight answers on what websites cost, why custom beats '
            'templates, and how to get found on Google — written by a '
            'CISSP-certified engineer who builds them.'
        ),
    })


def insight_detail(request, slug):
    """/insights/<slug>/ — one article."""
    from django.shortcuts import get_object_or_404
    from .models import Article
    article = get_object_or_404(Article, slug=slug, status='published')
    return render(request, 'public/insight_detail.html', {
        'active_nav': 'insights',
        'article': article,
        'breadcrumbs': [
            ('Insights', '/insights/'),
            (article.title, None),
        ],
    })


def service_law_firm_seo(request):
    """
    /services/seo/law-firm-seo/ — the highest-value page on the site.

    Owns `law firm seo` (4,400/mo) + `attorney seo` (3,600/mo) at
    $31-165 top-of-page bids. Per section 6.1 this page owns the
    law-firm SEO intent exclusively; /services/seo/local-seo/ must
    EXCLUDE those terms, and `lawyer marketing` (1,300/mo) is assigned
    here rather than to the law-firm design page.
    """
    return render(request, 'public/service_law_firm_seo.html', {
        'active_nav': 'services',
        'active_service': 'law_firm_seo',
        'breadcrumbs': [
            ('Services', '/services/seo/'),
            ('SEO', '/services/seo/'),
            ('Law Firm SEO', None),
        ],
    })


def service_law_firm_web_design(request):
    """
    /services/web-design/law-firm-web-design/ — section 7.3's spec page.

    Owns `law firm web design` (2,400) + `attorney website design`
    (1,900) + `lawyer website design` (1,900) + `legal web design`
    (480) + `law firm website` (390). Structure follows section 7.3:
    business outcomes first, code last.

    Distinct from /for-law-firms/ per D4 — that page is the vertical
    hub and owns the FindLaw switching conversation; this page owns
    the commercial search intent.
    """
    return render(request, 'public/service_law_firm_web_design.html', {
        'active_nav': 'services',
        'active_service': 'law_firm_web_design',
        'breadcrumbs': [
            ('Services', '/services/web-design/'),
            ('Web Design', '/services/web-design/'),
            ('Law Firm Web Design', None),
        ],
    })


def service_local_seo(request):
    """
    /services/seo/local-seo/ — biggest raw volume in the SEO cluster
    (`local seo` 14,800 + `local seo services` 8,100).

    Per section 6.1 this page must EXCLUDE law-firm SEO terms; those
    belong to /services/seo/law-firm-seo/. Crowded, agency-dominated
    SERP — the honest framing is the differentiator here.
    """
    return render(request, 'public/service_local_seo.html', {
        'active_nav': 'services',
        'active_service': 'local_seo',
        'breadcrumbs': [
            ('Services', '/services/seo/'),
            ('SEO', '/services/seo/'),
            ('Local SEO', None),
        ],
    })


def service_small_business_web_design(request):
    """
    /services/web-design/small-business-web-design/ — 3,600/mo on
    `small business web design`, LOW competition. On-brand and
    winnable.
    """
    return render(
        request, 'public/service_small_business_web_design.html', {
            'active_nav': 'services',
            'active_service': 'small_business_web_design',
            'breadcrumbs': [
                ('Services', '/services/web-design/'),
                ('Web Design', '/services/web-design/'),
                ('Small Business Web Design', None),
            ],
        })


def service_website_redesign(request):
    """
    /services/web-design/website-redesign/ — 3,600/mo, LOW
    competition, clean intent. The easiest win in the Phase 2 set.
    """
    return render(request, 'public/service_website_redesign.html', {
        'active_nav': 'services',
        'active_service': 'website_redesign',
        'breadcrumbs': [
            ('Services', '/services/web-design/'),
            ('Web Design', '/services/web-design/'),
            ('Website Redesign', None),
        ],
    })


def pricing(request):
    from billing.pricing_models import AddonPricing, ServiceTier
    return render(request, 'public/pricing.html', {
        'active_nav': 'pricing',
        'meta_title': 'Pricing — Aspired Websites',
        'meta_description': (
            'Transparent pricing for website builds, monthly maintenance, '
            'social media management, and hosting. Month-to-month, '
            'cancel anytime. No annual contracts.'
        ),
        'builds': ServiceTier.get_active('website_build'),
        'maintenance': ServiceTier.get_active('maintenance'),
        'social': ServiceTier.get_active('social_media'),
        'hosting': ServiceTier.get_active('hosting').first(),
        'addons': AddonPricing.objects.filter(is_active=True),
    })


# Layer 3 — bot-name + spam-content filters used by `_classify_spam`.
_SPAM_NAME_WORDS = {
    'casino', 'viagra', 'crypto', 'bitcoin', 'seo services', 'loan',
    'investment', 'earn money', 'work from home', 'click here',
    'free money',
}
_SPAM_EMAIL_DOMAINS = (
    'mail.ru', 'guerrillamail', 'mailinator', 'tempmail',
    'throwaway', 'yopmail', 'sharklasers', 'guerrillamailblock',
)

# ── What actually got through ──────────────────────────────────────────
#
# 35 of 35 contact-form leads on prod were spam, and every one cleared
# the four layers already here. Read before loosening any of this:
#
#   "RobertBiz"  x8   the same "I wanted to know your price" in Bulgarian,
#                     Latvian, Lithuanian, Igbo, Bosnian, Bengali,
#                     Italian and Spanish, from eight different IPs
#   "Andrew Walters"  "Dear Beloved, My name is Mr..." - a 419 advance-fee
#   "Jason Roberts" x3 identical SEO solicitation, same body each time
#   phishing@53.com   an actual submission
#
# The existing rules missed them for understandable reasons: the names
# are short and plausible, the bodies carry no URLs, and each IP only
# submitted once or twice so the per-IP caps never tripped. What they
# have in common is not volume from one source - it is the CONTENT, and
# in the multilingual case, the alphabet.

# A US web-design studio selling to Texas and Georgia law firms receives
# no genuine enquiries written in Cyrillic, Bengali or CJK. Latin-script
# languages are deliberately NOT covered here: Spanish-speaking Texas
# businesses are a real and wanted audience, and the Spanish/Italian
# variants of this bot get caught by the repeated-body rule instead.
_NON_LATIN_RANGES = (
    (0x0400, 0x04FF),   # Cyrillic
    (0x0500, 0x052F),   # Cyrillic supplement
    (0x0600, 0x06FF),   # Arabic
    (0x0900, 0x097F),   # Devanagari
    (0x0980, 0x09FF),   # Bengali
    (0x0E00, 0x0E7F),   # Thai
    (0x4E00, 0x9FFF),   # CJK
    (0x3040, 0x30FF),   # Kana
    (0xAC00, 0xD7AF),   # Hangul
)

# Cold B2B solicitation. These are pitches, not enquiries: the sender
# wants to sell TO Aspired rather than buy from it. Phrasing lifted from
# the real submissions.
_SOLICITATION_PHRASES = (
    'are you seeking', 'we specialize in offering',
    'high-quality backlink', 'backlink collabo', 'guest post',
    'link building', 'link exchange', 'dear beloved',
    'i am contacting you regarding my late', 'next of kin',
    'business proposal for you', 'i have a business proposal',
    'increase your website traffic', 'improve your google ranking',
    'first page of google', 'we can rank your', 'outsourcing partner',
    'white label seo', 'affordable seo', 'web design leads',
    'i wanted to know your price', 'i wanted to know my price',
)

# How many times the same normalised message body may be seen across ALL
# submitters before it is treated as a campaign rather than an enquiry.
# The eight-language bot varied its language and its IP but not its
# intent; three identical SEO pitches did not vary at all.
_REPEAT_BODY_LIMIT = 2
_REPEAT_BODY_WINDOW_SECS = 60 * 60 * 24 * 30


def _non_latin_ratio(text):
    """Share of letters outside the Latin alphabet, 0.0-1.0.

    Punctuation and digits are ignored so "Здравейте, 2026" is judged on
    its letters rather than diluted by its comma.
    """
    letters = [c for c in (text or '') if c.isalpha()]
    if not letters:
        return 0.0
    foreign = sum(
        1 for c in letters
        if any(lo <= ord(c) <= hi for lo, hi in _NON_LATIN_RANGES))
    return foreign / len(letters)


def _body_fingerprint(message):
    """Stable hash of a message, insensitive to trivial edits.

    Case, whitespace and punctuation are stripped before hashing so a
    bot that re-sends the same pitch with a different greeting still
    collides with itself.
    """
    import hashlib
    import re as _re
    normalised = _re.sub(r'[^a-z0-9]+', '', (message or '').lower())
    return hashlib.sha256(normalised.encode('utf-8')).hexdigest()[:32]


def _classify_spam(cleaned):
    """
    Layer 3 — content-based spam classifier.

    Returns a short reason string when the submission looks like spam,
    else empty string. Each rule is conservative on its own and the
    operator can see why anything was suppressed via the server log.
    """
    name = (cleaned.get('name') or '').strip()
    email = (cleaned.get('email') or '').strip().lower()
    message = (cleaned.get('message') or '').strip()
    lower_name = name.lower()
    lower_msg = message.lower()

    # >3 URLs in the message — classic linkspam tell.
    url_count = lower_msg.count('http://') + lower_msg.count('https://')
    if url_count > 3:
        return f'message has {url_count} URLs'

    # Spam keyword in name (case-insensitive substring).
    for word in _SPAM_NAME_WORDS:
        if word in lower_name or word in lower_msg:
            return f'spam keyword: {word!r}'

    # Throwaway / known-spam email domain.
    if email and '@' in email:
        domain = email.rsplit('@', 1)[-1]
        for bad in _SPAM_EMAIL_DOMAINS:
            if bad in domain:
                return f'spam email domain: {domain}'

    # Too short to be a real inquiry.
    if len(message) < 20:
        return f'message too short ({len(message)} chars)'

    # Bot name pattern: a single CamelCase word like "LloydSit" — no
    # spaces, longer than 20 chars, mixed case. Real names with no
    # spaces under 20 chars (e.g. "Mike") fall through cleanly.
    if name and ' ' not in name and len(name) > 20:
        return f'bot-name pattern: {name!r}'

    # Non-Latin script. A studio selling to Texas and Georgia law firms
    # gets no genuine enquiries in Cyrillic or Bengali. Spanish and
    # Italian are Latin and therefore untouched here on purpose —
    # Spanish-speaking Texas businesses are a wanted audience.
    ratio = _non_latin_ratio(message)
    if ratio > 0.30:
        return f'non-Latin script ({ratio:.0%} of letters)'

    # Cold B2B solicitation — someone selling TO us, not asking to buy.
    for phrase in _SOLICITATION_PHRASES:
        if phrase in lower_msg:
            return f'solicitation phrase: {phrase!r}'

    # A name that is one word ending in a business-ish suffix, e.g.
    # "RobertBiz". Real people rarely introduce themselves that way.
    if name and ' ' not in name:
        for suffix in ('biz', 'seo', 'marketing', 'agency', 'media',
                       'digital', 'promo', 'ads'):
            if lower_name.endswith(suffix) and len(name) > len(suffix) + 2:
                return f'bot-name suffix: {name!r}'

    return ''


def _is_repeated_body(message):
    """Has this exact pitch been sent before, by anyone?

    The per-IP caps assume a bot hammers from one address. The one that
    beat them sent eight times from eight IPs, varying only the
    language. What did not vary was the intent, and after normalisation
    the SEO pitches did not vary at all.

    Counts across every submitter, so a genuine second enquiry from the
    same person is unaffected — nobody sends the same paragraph twice.
    Returns (is_repeat, count_seen_before).
    """
    from django.core.cache import cache

    if not (message or '').strip():
        return False, 0
    key = f'contact_body:{_body_fingerprint(message)}'
    seen = cache.get(key, 0)
    cache.set(key, seen + 1, _REPEAT_BODY_WINDOW_SECS)
    return seen >= _REPEAT_BODY_LIMIT, seen


def _signed_form_timestamp():
    """Return a signed `int(time.time())` for the honeypot timing check."""
    import time as _time
    from django.core.signing import dumps
    return dumps(int(_time.time()))


def _form_age_seconds(signed_value):
    """
    Decode a previously-issued timestamp. Returns (age_seconds, ok)
    where ok=False when the signature is bad or the token has expired
    (>2h old). Callers treat ok=False as definitely-spam.
    """
    import time as _time
    from django.core.signing import BadSignature, SignatureExpired, loads
    try:
        rendered_at = loads(signed_value, max_age=7200)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return 0, False
    return _time.time() - int(rendered_at), True


def _silently_pretend_success(request):
    """
    Spam handler — every layer returns this. Visually identical to a
    real success so the bot has no signal that anything was filtered.
    No Lead row, no admin email.
    """
    return redirect('public:contact_thanks')


@ratelimit(key='ip', rate='5/h', method='POST', block=False)
def contact(request):
    import logging
    from django.core.cache import cache
    logger = logging.getLogger(__name__)

    rate_limited = getattr(request, 'limited', False)

    if request.method == 'POST':
        ip = _client_ip(request) or ''

        # Layer 4 — strict per-IP cap: max 3 contact submissions / hour.
        # Lives next to the existing django-ratelimit decorator (which
        # is 5/h) so this layer absorbs the short bot-burst attacks
        # even when ratelimit's window hasn't ticked yet.
        cache_key = f'contact_form:{ip}'
        per_ip_count = cache.get(cache_key, 0)
        if per_ip_count >= 3:
            logger.info(
                'SPAM BLOCKED (rate-limit IP=%s count=%s)',
                ip, per_ip_count)
            return _silently_pretend_success(request)

        # Layer 1 — honeypot: real users never see the `website_url`
        # field (offscreen, tab-index -1, no autocomplete). Anything
        # in there is a bot.
        if (request.POST.get('website_url') or '').strip():
            logger.info(
                'SPAM BLOCKED (honeypot IP=%s)', ip)
            cache.set(cache_key, per_ip_count + 1, 3600)
            return _silently_pretend_success(request)

        # Layer 2 — form-age check. Bots submit instantly; humans take
        # at least a few seconds. Missing/expired token is treated as
        # spam too.
        signed_ts = (request.POST.get('form_timestamp') or '').strip()
        age, ok = _form_age_seconds(signed_ts)
        if not ok or age < 3:
            logger.info(
                'SPAM BLOCKED (timing IP=%s ok=%s age=%.1fs)',
                ip, ok, age)
            cache.set(cache_key, per_ip_count + 1, 3600)
            return _silently_pretend_success(request)

        form = ContactForm(request.POST)
        if rate_limited:
            form.add_error(
                None,
                'You’ve sent too many messages from this network in the last hour. '
                'Please try again later or call/text us directly at 210-896-2536.',
            )
        elif form.is_valid():
            # Layer 3 — content classifier. Runs on validated form
            # data so the regex / domain checks don't choke on raw
            # POST garbage.
            reason = _classify_spam(form.cleaned_data)
            if reason:
                logger.info(
                    'SPAM BLOCKED (content IP=%s reason=%s '
                    'email=%s)',
                    ip, reason,
                    form.cleaned_data.get('email', '?'))
                cache.set(cache_key, per_ip_count + 1, 3600)
                return _silently_pretend_success(request)

            # Layer 5 — the same pitch, from anyone. Runs after the
            # content classifier so a message already rejected does not
            # consume a fingerprint slot, and only on otherwise-valid
            # submissions so raw POST garbage cannot poison the cache.
            repeated, seen_before = _is_repeated_body(
                form.cleaned_data.get('message', ''))
            if repeated:
                logger.info(
                    'SPAM BLOCKED (repeat body IP=%s seen=%s email=%s)',
                    ip, seen_before, form.cleaned_data.get('email', '?'))
                cache.set(cache_key, per_ip_count + 1, 3600)
                return _silently_pretend_success(request)

            ref_code = (request.session.get('referral_code') or '').strip()
            lead = form.save_as_lead(
                ip_address=ip or None,
                referral_code=ref_code,
            )
            if ref_code:
                # Best-effort: stamp the lead, bump counters, log event.
                from clients.views import credit_referral_for_lead
                try:
                    credit_referral_for_lead(lead, ref_code)
                except Exception:  # noqa: BLE001 — never break contact form
                    pass
            _send_lead_auto_reply(lead)
            _send_lead_internal_notification(lead)
            # §10 conversion. Queued only here — past every spam layer
            # and after the Lead row exists — so the count in GA4 is
            # real leads, not submissions. _silently_pretend_success
            # renders the same thanks page for bots and queues nothing.
            # Category fields only: name, email and phone are PII and
            # core.analytics refuses them.
            queue_event(
                request, 'contact_form_submit',
                # service_interest was specified in MEASUREMENT_SPEC §5
                # from the start; the form had no field for it, so it
                # shipped as a documented deviation. The field exists
                # now, so the event carries what the spec asked for.
                service_interest=form.cleaned_data.get(
                    'service_interest', ''),
                business_type=form.cleaned_data.get('business_type', ''),
                heard_about=form.cleaned_data.get('source', ''),
                page_path=request.path,
            )
            # Count a successful submit against the IP cap too — three
            # legit submissions in an hour is plenty.
            cache.set(cache_key, per_ip_count + 1, 3600)
            return redirect('public:contact_thanks')
    else:
        form = ContactForm(initial={
            'form_timestamp': _signed_form_timestamp(),
        })

    return render(request, 'public/contact.html', {
        'active_nav': 'contact',
        'form': form,
        'meta_title': 'Contact — Aspired Websites',
        'meta_description': (
            'Get in touch about your project. Free consultation, no obligation. '
            f'{LOCATION_STATEMENT}'
        ),
    })


def contact_thanks(request):
    return render(request, 'public/thanks.html', {
        'active_nav': 'contact',
        'meta_title': 'Message Received — Aspired Websites',
        'meta_description': 'Thanks — we’ll be in touch within 24 hours.',
    })


def _client_ip(request):
    """Best-effort client IP. Honors X-Forwarded-For when behind a proxy."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _send_lead_auto_reply(lead):
    body = (
        f'Hi {lead.attorney_name},\n\n'
        f'Thanks for reaching out — I got your message and will be back in touch '
        f'within 24 hours.\n\n'
        f'In the meantime, feel free to call or text me directly at 210-896-2536.\n\n'
        f'— Zachery Long\n'
        f'Aspired Websites LLC\n'
        f'aspiredwebsites.com\n'
    )
    send_mail(
        subject='Got your message — Aspired Websites',
        message=body,
        from_email=settings.EMAIL_FROM_CONTACT,
        recipient_list=[lead.email],
        fail_silently=True,
    )


def _send_lead_internal_notification(lead):
    body = (
        f'New lead from {lead.firm_name}.\n\n'
        f'Name:          {lead.attorney_name}\n'
        f'Business:      {lead.firm_name}\n'
        f'Business type: {lead.business_type}\n'
        f'Needs:         {lead.service_interest or "Not specified"}\n'
        f'Phone:         {lead.phone}\n'
        f'Email:         {lead.email}\n'
        f'Heard about:   {lead.tags or "Not specified"}\n'
        f'IP address:    {lead.ip_address or "unknown"}\n'
        f'Submitted at:  {lead.created_at:%Y-%m-%d %H:%M:%S %Z}\n\n'
        f'Message:\n'
        f'{"-" * 60}\n'
        f'{lead.inquiry_text}\n'
    )
    send_mail(
        subject=f'New Lead: {lead.firm_name} — {lead.business_type}',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


def about(request):
    return render(request, 'public/about.html', {
        'active_nav': 'about',
        'meta_title': 'About Zachery Long — Aspired Websites',
        'meta_description': (
            'Aspired Websites is built by Zachery Long — CISSP-certified, '
            f'M.S. in Cybersecurity, {LOCATION_PHRASE}. '
            'Direct access, no outsourcing, security-first.'
        ),
    })


@ratelimit(key='ip', rate='3/h', method='POST', block=False)
def audit(request):
    rate_limited = getattr(request, 'limited', False)

    if request.method == 'POST':
        form = AuditForm(request.POST)
        if rate_limited:
            form.add_error(
                None,
                'You’ve run too many audits in the last hour. '
                'Please try again later or call us at 210-896-2536 for a manual review.',
            )
        elif form.is_valid():
            url = form.cleaned_data['url']
            try:
                result = _run_pagespeed_audit(url)
            except _PageSpeedError as err:
                form.add_error('url', str(err))
            else:
                request.session['audit_url'] = url
                request.session['audit_scores'] = result['scores']
                request.session['audit_issues'] = result['issues_by_category']
                request.session.pop('audit_email_submitted', None)
                # Drop any AI review cached from a previous audit run.
                request.session.pop('audit_ai_review', None)
                # §10 conversion — the audit actually ran and produced
                # scores. A failed PageSpeed call takes the except
                # branch above and queues nothing. The audited domain
                # is a business domain, not visitor PII (§5.3).
                queue_event(
                    request, 'audit_request',
                    audited_domain=_domain_of(url),
                    page_path=request.path,
                )
                return redirect('public:audit_results')
    else:
        form = AuditForm()

    return render(request, 'public/audit.html', {
        'active_nav': 'audit',
        'form': form,
        'meta_title': 'Free Website Audit — Aspired Websites',
        'meta_description': (
            'Free website audit. Speed, SEO, accessibility, best-practices '
            'scores in under 30 seconds. No email required.'
        ),
    })


def audit_results(request):
    audit_url = request.session.get('audit_url')
    scores = request.session.get('audit_scores')
    issues_by_category = request.session.get('audit_issues')
    if not isinstance(issues_by_category, dict):
        issues_by_category = {}

    if not (audit_url and scores):
        return redirect('public:audit')

    # POST: email capture
    email_form = AuditEmailForm()
    if request.method == 'POST':
        email_form = AuditEmailForm(request.POST)
        if email_form.is_valid():
            audit_lead = AuditLead.objects.create(
                url=audit_url,
                performance_score=scores['performance'],
                seo_score=scores['seo'],
                best_practices_score=scores['best_practices'],
                accessibility_score=scores['accessibility'],
                issues=issues_by_category,
                email=email_form.cleaned_data['email'],
                ip_address=_client_ip(request),
            )
            # Email 1 of the follow-up sequence, sent immediately.
            # Was an inline sender with no unsubscribe link, which made
            # the message this business sends most often the one message
            # that was not CAN-SPAM compliant.
            from public.audit_sequence import send_report
            try:
                send_report(audit_lead)
            except Exception:  # noqa: BLE001 - never break the capture
                logger.exception(
                    'audit report send failed for %s', audit_lead.pk)
            request.session['audit_email_submitted'] = True
            # §10 conversion — this one creates a contactable AuditLead,
            # so it is the audit funnel's real finish line. The visitor's
            # email is deliberately NOT a param (§5.3); only the domain
            # they audited, which is a business, not a person.
            queue_event(
                request, 'audit_email_capture',
                audited_domain=_domain_of(audit_url),
            )
            return redirect('public:audit_results')

    def status_for(s):
        if s >= 90:
            return 'good'
        if s >= 50:
            return 'ok'
        return 'bad'

    score_cards = [
        {'label': 'Performance',    'score': scores['performance'],    'status': status_for(scores['performance'])},
        {'label': 'SEO',            'score': scores['seo'],            'status': status_for(scores['seo'])},
        {'label': 'Best Practices', 'score': scores['best_practices'], 'status': status_for(scores['best_practices'])},
        {'label': 'Accessibility',  'score': scores['accessibility'],  'status': status_for(scores['accessibility'])},
    ]

    # Four detailed result cards — one per category, always shown, in order.
    result_cards = []
    for key in ('performance', 'seo', 'best_practices', 'accessibility'):
        score = scores[key]
        tier = _score_tier(score)
        is_clear = score >= 90
        result_cards.append({
            'label':      _CATEGORY_LABELS[key],
            'score':      score,
            'tier':       tier,
            'tier_label': _TIER_LABELS[tier],
            'impact':     _IMPACT_STATEMENTS[key][tier],
            'is_clear':   is_clear,
            'issues':     [] if is_clear else (issues_by_category.get(key) or [])[:2],
        })

    return render(request, 'public/audit_results.html', {
        'active_nav': 'audit',
        'audit_url': audit_url,
        'score_cards': score_cards,
        'result_cards': result_cards,
        'audit_summary': _audit_summary(audit_url, scores),
        'email_form': email_form,
        'email_submitted': bool(request.session.get('audit_email_submitted')),
        'meta_title': f'Audit Results for {audit_url} — Aspired Websites',
    })


def _audit_summary(audit_url, scores):
    """Build the one-line overall summary shown above the result cards."""
    parsed = urlparse(audit_url)
    domain = (parsed.netloc or parsed.path or audit_url).rstrip('/')
    if domain.startswith('www.'):
        domain = domain[4:]

    values = list(scores.values())
    if any(s < 50 for s in values):
        return {
            'tier': 'critical',
            'text': f'{domain} has critical issues that need immediate attention.',
        }
    needs_work = sum(1 for s in values if s < 90)
    if needs_work:
        noun = 'area' if needs_work == 1 else 'areas'
        verb = 'needs' if needs_work == 1 else 'need'
        return {
            'tier': 'needs-work',
            'text': f'{domain} has {needs_work} {noun} that {verb} attention.',
        }
    return {
        'tier': 'strong',
        'text': f'{domain} is performing well across all areas.',
    }


# ── PageSpeed Insights helpers ──────────────────────────────────────────────

class _PageSpeedError(Exception):
    """User-facing error for audit failures."""


def _run_pagespeed_audit(url):
    """
    Run the website audit and return
    {'scores': {...}, 'issues_by_category': {...}}.
    Raises _PageSpeedError with a user-facing message on failure.
    """
    # PageSpeed returns only the Performance category by default — request
    # all four explicitly. The `category` param accepts multiple values.
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

    try:
        response = requests.get(
            PAGESPEED_API_URL,
            params=params,
            timeout=PAGESPEED_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise _PageSpeedError(
            'The audit took too long. Try again in a moment — '
            'or send us the URL directly at 210-896-2536.'
        )
    except requests.RequestException:
        raise _PageSpeedError(
            'Couldn’t reach the audit service. Please try again in a minute.'
        )

    if response.status_code != 200:
        # Google returns 400 for unreachable URLs, 429 for rate-limit.
        raise _PageSpeedError(
            'We couldn’t analyze that URL. Double-check it loads in a browser '
            'and try again.'
        )

    try:
        data = response.json()
    except ValueError:
        raise _PageSpeedError('Got an unexpected response from the audit service.')

    lighthouse = data.get('lighthouseResult') or {}
    categories = lighthouse.get('categories') or {}
    audits = lighthouse.get('audits') or {}

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

    issues_by_category = {
        key: _category_issues(categories.get(lh_key) or {}, audits)
        for key, lh_key in _CATEGORY_KEYS.items()
    }

    return {'scores': scores, 'issues_by_category': issues_by_category}


def _category_issues(category, audits):
    """
    Pull up to 2 actionable, plain-English issues for one audit category.

    An audit counts as an issue when it failed (score below 0.9) and is
    something a site owner can act on — a performance "opportunity" or a
    binary pass/fail check (the form most SEO, accessibility, and
    best-practice audits take). Metrics and informational diagnostics are
    skipped: they have scores but aren't directly fixable.
    """
    found = []
    for ref in category.get('auditRefs') or []:
        audit_data = audits.get(ref.get('id')) or {}
        score = audit_data.get('score')
        if score is None or score >= 0.9:
            continue
        details = audit_data.get('details') or {}
        actionable = (
            details.get('type') == 'opportunity'
            or audit_data.get('scoreDisplayMode') == 'binary'
        )
        if not actionable:
            continue
        title = audit_data.get('title')
        if not title:
            continue
        # Strip markdown link syntax [text](url) → text.
        description = re.sub(
            r'\[([^\]]+)\]\([^)]+\)', r'\1', audit_data.get('description') or ''
        ).strip()
        found.append({
            'title': title,
            'description': description,
            'score': score,
        })
    found.sort(key=lambda item: item['score'])
    return found[:2]


def _flatten_issues(issues_by_category):
    """Flatten the per-category issues dict into one ordered list."""
    if not isinstance(issues_by_category, dict):
        return []
    flat = []
    for key in ('performance', 'seo', 'best_practices', 'accessibility'):
        flat.extend(issues_by_category.get(key) or [])
    return flat


def audit_ai_review(request):
    """
    HTMX partial endpoint. Generates (or returns cached) AI-written
    plain-English review of the audit results stored in the session.
    Falls back gracefully if the API key is missing or the call fails.
    """
    scores = request.session.get('audit_scores')
    audit_url = request.session.get('audit_url')
    if not (scores and audit_url):
        return render(request, 'public/_audit_ai_review.html', {
            'fallback': 'Run an audit first to see your AI review.',
        })

    cached = request.session.get('audit_ai_review')
    if cached:
        return render(request, 'public/_audit_ai_review.html', {
            'review_paragraphs': cached.split('\n\n'),
        })

    if not settings.ANTHROPIC_API_KEY:
        return render(request, 'public/_audit_ai_review.html', {
            'fallback': (
                'AI review unavailable right now. '
                'Book a call below and we’ll walk you through these results in plain English.'
            ),
        })

    issues = _flatten_issues(request.session.get('audit_issues'))
    try:
        review = _generate_ai_audit_review(audit_url, scores, issues)
    except Exception:
        return render(request, 'public/_audit_ai_review.html', {
            'fallback': (
                'AI review couldn’t run right now. '
                'Book a call below and we’ll walk through these results with you.'
            ),
        })

    request.session['audit_ai_review'] = review
    return render(request, 'public/_audit_ai_review.html', {
        'review_paragraphs': review.split('\n\n'),
    })


def _generate_ai_audit_review(url, scores, issues):
    """Generate a plain-English audit review via the AI agent."""
    # Local import so the public app doesn't hard-depend on anthropic at
    # module load time — keeps Django startup fast and lets the rest of
    # the app run even if the SDK is broken/missing.
    from anthropic import Anthropic

    if issues:
        issue_lines = []
        for issue in issues[:8]:
            issue_lines.append(
                f"- {issue.get('title', '')}: "
                f"{issue.get('description', '')[:220]}"
            )
        issue_block = '\n'.join(issue_lines)
    else:
        issue_block = '(No major opportunities detected — site is already solid.)'

    prompt = f"""You are reviewing a website audit for a small business owner who probably doesn't know what most of these scores actually mean. Based on the audit results below, write a 2-3 paragraph plain-English review that:

1. Translates what the scores actually mean for their business — visitors lost, slow page loads, missed leads, conversion impact. Be specific about real-world consequences.
2. Identifies the single most important issue to fix first and why it matters.
3. Says what they should do next.

Voice:
- Conversational and direct
- Honest about bad scores — don't sugarcoat
- No jargon, no acronyms (PageSpeed, Lighthouse, FCP, LCP) — translate them
- Write like you're explaining to a small business owner who just hired you
- 200-350 words total
- Paragraphs separated by a blank line
- No markdown headings, no bullet points, no asterisks — just clean flowing paragraphs

WEBSITE: {url}

SCORES (mobile, 0-100):
- Performance: {scores['performance']}
- SEO: {scores['seo']}
- Best Practices: {scores['best_practices']}
- Accessibility: {scores['accessibility']}

TOP ACTIONABLE OPPORTUNITIES:
{issue_block}

Write the review now."""

    model = 'claude-haiku-4-5-20251001'
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=model,
        max_tokens=900,
        messages=[{'role': 'user', 'content': prompt}],
    )
    # Token accounting → admin dashboard AI Usage widget.
    # Best-effort: a DB hiccup must not break the audit review.
    try:
        from reporting.models import ClaudeUsage
        u = getattr(message, 'usage', None)
        if u is not None:
            ClaudeUsage.record(
                model=model,
                input_tokens=getattr(u, 'input_tokens', 0),
                output_tokens=getattr(u, 'output_tokens', 0))
    except Exception:
        # No logger imported in this module; fail silent rather
        # than crash an unrelated logger setup.
        pass
    return message.content[0].text.strip()


# _send_audit_report was removed on 2026-08-23. It built the report
# inline with no unsubscribe link and no postal address, which made the
# message this business sends most often the one message that was not
# CAN-SPAM compliant. public/audit_sequence.py replaces it and carries
# both, on all three emails.
@ratelimit(key='post:email', rate='5/h', method='POST', block=False)
@ratelimit(key='ip',         rate='10/h', method='POST', block=False)
def login_page(request):
    """
    Unified login. Admin staff land on /admin-dashboard/, everyone else on
    the client portal (currently the coming-soon placeholder).

    Auth lookup is by email — we resolve to the actual User by email then
    authenticate with their username + password (Django's default backend
    is username-based).
    """
    # Already signed in? Bounce them.
    if request.user.is_authenticated:
        return _post_login_redirect(request.user, request.GET.get('next', ''))

    error = None
    next_url = request.POST.get('next') or request.GET.get('next') or ''

    if request.method == 'POST':
        rate_limited = getattr(request, 'limited', False)
        if rate_limited:
            error = (
                'Too many login attempts. Please try again later, '
                'or call 210-896-2536 if you’re locked out.'
            )
        else:
            email = (request.POST.get('email') or '').strip()
            password = request.POST.get('password') or ''
            user = _authenticate_by_email(request, email, password)
            if user is not None:
                login(request, user)
                return _post_login_redirect(user, next_url)
            error = 'Invalid email or password.'

    is_admin_login = next_url.startswith('/admin-dashboard')

    return render(request, 'public/login.html', {
        'active_nav': 'login',
        'meta_title': 'Sign In — Aspired Websites',
        'meta_description': 'Sign in to your Aspired Websites account.',
        'error': error,
        'next': next_url,
        'is_admin_login': is_admin_login,
    })


@require_POST
def logout_view(request):
    """POST-only logout (modern Django requires POST for CSRF-safe logout).

    Clears the Phase C ``active_website_slug`` session pick before
    flushing the rest of the session so a re-login lands on a fresh
    chooser, not whatever site was picked last.
    """
    try:
        from clients.portal_resolvers import clear_active_website
        clear_active_website(request)
    except Exception:
        # Never block logout over a helper import — fail open.
        pass
    logout(request)
    return redirect('public:home')


def _authenticate_by_email(request, email, password):
    """Look up user by email (case-insensitive), authenticate by username+pw."""
    if not email or not password:
        return None
    User = get_user_model()
    user_row = User.objects.filter(email__iexact=email).first()
    if user_row is None:
        return None
    return authenticate(request, username=user_row.username, password=password)


def _post_login_redirect(user, next_url):
    """Resolve safe redirect target post-login."""
    # Honor ?next= if it's a same-origin URL (no open-redirect risk).
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts=None, require_https=False
    ):
        return redirect(next_url)
    # Staff → admin dashboard.
    if user.is_staff:
        return redirect('admin_dashboard:home')
    # Phase C — every fresh client sign-in lands on the website chooser.
    # The chooser auto-redirects to the dashboard for single-website
    # accounts (so single-site users never see an interstitial); accounts
    # with multiple sites get the picker. Legacy accounts with no
    # backfill (no Account row) fall back to /portal/ which renders the
    # legacy dashboard via the same view.
    return redirect('clients:chooser')


def portal_coming_soon(request):
    return render(request, 'public/portal_coming_soon.html', {
        'active_nav': 'login',
        'meta_title': 'Portal Coming Soon — Aspired Websites',
    })


def domain_parked(request):
    """
    Landing page for cancelled-hosting domains whose DNS now redirects
    here. ?for=clientdomain.com tells us which domain so we can show
    it on the page. We do NOT trust the param — only display it after
    a strict shape check; the page never queries any client info.
    """
    raw = (request.GET.get('for') or '').strip().lower()
    # Hostname-shape check — letters, digits, dots, hyphens only.
    safe_for = ''
    if raw and 0 < len(raw) <= 253:
        if all(c.isalnum() or c in '.-' for c in raw):
            safe_for = raw
    return render(request, 'public/domain_parked.html', {
        'active_nav': '',
        'for_domain': safe_for,
        'meta_title': 'Site offline — Aspired Websites',
    })


def audit_unsubscribe(request, token):
    """One-click opt-out from the audit follow-up sequence.

    No login, no confirmation step, no "are you sure". A recipient who
    clicks unsubscribe has already decided, and every extra step between
    them and being gone is another chance they press the spam button
    instead -- which costs the sending domain far more than the
    unsubscribe ever would.

    Also adds the address to the global SuppressionList, so opting out
    here means opting out of cold outreach too. Somebody who says no is
    saying no to us, not to one particular mailing.
    """
    from django.utils import timezone

    from outreach.models import SuppressionList
    from public.audit_sequence import resolve_unsubscribe_token

    audit_lead = resolve_unsubscribe_token(token)

    if audit_lead is not None and not audit_lead.unsubscribed:
        audit_lead.unsubscribed = True
        audit_lead.unsubscribed_at = timezone.now()
        audit_lead.save(update_fields=['unsubscribed', 'unsubscribed_at'])
        if audit_lead.email:
            SuppressionList.objects.get_or_create(
                email=audit_lead.email.lower(),
                defaults={
                    'reason': 'unsubscribed (audit follow-up)',
                    'domain': audit_lead.email.rpartition('@')[2],
                },
            )

    # The same page renders whether or not the token resolved. A bad or
    # already-used token still says "you are unsubscribed", because the
    # alternative is telling somebody their opt-out failed when there is
    # nothing they can do about it.
    return render(request, 'public/audit_unsubscribed.html', {
        'meta_title': 'Unsubscribed — Aspired Websites',
        'meta_description': 'You have been removed from our list.',
        'noindex': True,
    })
