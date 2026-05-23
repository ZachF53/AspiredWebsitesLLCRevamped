import re
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .forms import AuditEmailForm, AuditForm, ContactForm
from .models import AuditLead


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
    return render(request, 'public/home.html', {
        'active_nav': 'home',
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

    return render(request, 'public/law_firms.html', {
        'active_nav': 'law_firms',
        'meta_title': 'Custom Websites for Law Firms',
        'meta_description': (
            'Hand-coded, security-hardened websites built specifically '
            'for law firms. CISSP-certified, state bar compliant. '
            'No FindLaw lock-in, no templates.'
        ),
        'build_range': _price_range('website_build'),
        'maintenance_range': _price_range('maintenance'),
    })


def portfolio(request):
    return render(request, 'public/portfolio.html', {
        'active_nav': 'portfolio',
        'meta_title': 'Portfolio — Aspired Websites',
        'meta_description': (
            'Recent work by Aspired Websites: Denis Law Group, '
            'Food Trucks of San Antonio, Moonieful Designs, and '
            'Burgland Technologies. Hand-coded, mobile-first.'
        ),
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

    return ''


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
            'Based in San Antonio and Atlanta.'
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
            'M.S. in Cybersecurity, based in San Antonio and Atlanta. '
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
            AuditLead.objects.create(
                url=audit_url,
                performance_score=scores['performance'],
                seo_score=scores['seo'],
                best_practices_score=scores['best_practices'],
                accessibility_score=scores['accessibility'],
                issues=issues_by_category,
                email=email_form.cleaned_data['email'],
                ip_address=_client_ip(request),
            )
            _send_audit_report(
                audit_url, scores, issues_by_category,
                email_form.cleaned_data['email'],
            )
            request.session['audit_email_submitted'] = True
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

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=900,
        messages=[{'role': 'user', 'content': prompt}],
    )
    return message.content[0].text.strip()


def _send_audit_report(url, scores, issues_by_category, email):
    if not isinstance(issues_by_category, dict):
        issues_by_category = {}

    lines = [
        f'Here are the full audit results for {url}:',
        '',
    ]
    for key in ('performance', 'seo', 'best_practices', 'accessibility'):
        lines.append(f'{_CATEGORY_LABELS[key]}: {scores[key]}/100')
        for issue in issues_by_category.get(key) or []:
            lines.append(f'  - {issue.get("title", "")}')
            if issue.get('description'):
                lines.append(f'    {issue["description"]}')
        lines.append('')

    lines += [
        '---',
        '',
        'Want to fix all of this? Book a free 30-minute call:',
        'https://aspiredwebsites.com/contact/',
        '',
        '— Zachery Long',
        'Aspired Websites LLC',
    ]

    send_mail(
        subject=f'Your website audit: {url}',
        message='\n'.join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=True,
    )


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
    """POST-only logout (modern Django requires POST for CSRF-safe logout)."""
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
    # Staff → admin dashboard. Everyone else → client portal.
    if user.is_staff:
        return redirect('admin_dashboard:home')
    return redirect('clients:dashboard')


def portal_coming_soon(request):
    return render(request, 'public/portal_coming_soon.html', {
        'active_nav': 'login',
        'meta_title': 'Portal Coming Soon — Aspired Websites',
    })
