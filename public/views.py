import re

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django_ratelimit.decorators import ratelimit

from .forms import AuditEmailForm, AuditForm, ContactForm
from .models import AuditLead


PAGESPEED_API_URL = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
PAGESPEED_TIMEOUT_SECONDS = 45


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
    return render(request, 'public/law_firms.html', {
        'active_nav': 'law_firms',
        'meta_title': 'Custom Websites for Law Firms',
        'meta_description': (
            'Hand-coded, security-hardened websites built specifically '
            'for law firms. CISSP-certified, state bar compliant. '
            'No FindLaw lock-in, no templates.'
        ),
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
    return render(request, 'public/pricing.html', {
        'active_nav': 'pricing',
        'meta_title': 'Pricing — Aspired Websites',
        'meta_description': (
            'Transparent pricing. Website builds from $2,500. '
            'Monthly maintenance from $299. Month-to-month, '
            'cancel anytime. No annual contracts.'
        ),
    })


@ratelimit(key='ip', rate='5/h', method='POST', block=False)
def contact(request):
    rate_limited = getattr(request, 'limited', False)

    if request.method == 'POST':
        form = ContactForm(request.POST)
        if rate_limited:
            form.add_error(
                None,
                'You’ve sent too many messages from this network in the last hour. '
                'Please try again later or call/text us directly at 210-896-2536.',
            )
        elif form.is_valid():
            lead = form.save(commit=False)
            lead.ip_address = _client_ip(request)
            lead.status = 'new'
            lead.save()
            _send_lead_auto_reply(lead)
            _send_lead_internal_notification(lead)
            return redirect('public:contact_thanks')
    else:
        form = ContactForm()

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
        f'Hi {lead.name},\n\n'
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
        f'New lead from {lead.business_name}.\n\n'
        f'Name:          {lead.name}\n'
        f'Business:      {lead.business_name}\n'
        f'Business type: {lead.get_business_type_display()}\n'
        f'Phone:         {lead.phone}\n'
        f'Email:         {lead.email}\n'
        f'Source:        {lead.get_source_display() or "Not specified"}\n'
        f'IP address:    {lead.ip_address or "unknown"}\n'
        f'Submitted at:  {lead.created_at:%Y-%m-%d %H:%M:%S %Z}\n\n'
        f'Message:\n'
        f'{"-" * 60}\n'
        f'{lead.message}\n'
    )
    send_mail(
        subject=f'New Lead: {lead.business_name} — {lead.get_business_type_display()}',
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
                request.session['audit_issues'] = result['issues']
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
    issues = request.session.get('audit_issues') or []

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
                issues=issues,
                email=email_form.cleaned_data['email'],
                ip_address=_client_ip(request),
            )
            _send_audit_report(audit_url, scores, issues, email_form.cleaned_data['email'])
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

    # Pad the issues list to 3 with positive placeholders
    issues_padded = list(issues)
    while len(issues_padded) < 3:
        issues_padded.append({
            'title': 'Your site is performing well in this area',
            'description': 'No significant issues detected — keep doing what you’re doing.',
            'impact': 'good',
        })

    return render(request, 'public/audit_results.html', {
        'active_nav': 'audit',
        'audit_url': audit_url,
        'score_cards': score_cards,
        'issues': issues_padded[:3],
        'email_form': email_form,
        'email_submitted': bool(request.session.get('audit_email_submitted')),
        'meta_title': f'Audit Results for {audit_url} — Aspired Websites',
    })


# ── PageSpeed Insights helpers ──────────────────────────────────────────────

class _PageSpeedError(Exception):
    """User-facing error for audit failures."""


def _run_pagespeed_audit(url):
    """
    Call Google PageSpeed Insights and return {'scores': {...}, 'issues': [...]}.
    No API key required for low-volume use (25k queries/day, 1 qps).
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

    # Surface only "opportunities" — actionable fixes (e.g. "Reduce unused CSS").
    # This filters out metrics (Speed Index, FCP) and informational diagnostics
    # which have scores but aren't things the user can directly act on.
    issues = []
    for audit_id, audit_data in audits.items():
        score = audit_data.get('score')
        if score is None or score >= 0.9:
            continue
        details = audit_data.get('details') or {}
        if details.get('type') != 'opportunity':
            continue
        title = audit_data.get('title')
        if not title:
            continue
        description = audit_data.get('description', '') or ''
        # Strip markdown link syntax [text](url) → text.
        clean_desc = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', description).strip()
        issues.append({
            'title': title,
            'description': clean_desc,
            'score': score,
            'impact': 'high' if score < 0.5 else 'medium',
        })
    issues.sort(key=lambda x: x['score'])
    return {'scores': scores, 'issues': issues[:3]}


def audit_ai_review(request):
    """
    HTMX partial endpoint. Generates (or returns cached) Claude-written
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

    issues = request.session.get('audit_issues') or []
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
    """Call Anthropic Claude (Haiku 4.5) for a plain-English audit review."""
    # Local import so the public app doesn't hard-depend on anthropic at
    # module load time — keeps Django startup fast and lets the rest of
    # the app run even if the SDK is broken/missing.
    from anthropic import Anthropic

    if issues:
        issue_lines = []
        for issue in issues[:3]:
            impact = (issue.get('impact') or '').title()
            issue_lines.append(
                f"- {issue.get('title', '')} ({impact} impact): "
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


def _send_audit_report(url, scores, issues, email):
    lines = [
        f'Here are the full audit results for {url}:',
        '',
        f'Performance:    {scores["performance"]}/100',
        f'SEO:            {scores["seo"]}/100',
        f'Best Practices: {scores["best_practices"]}/100',
        f'Accessibility:  {scores["accessibility"]}/100',
        '',
        'Top issues:',
    ]
    for i, issue in enumerate(issues or [], 1):
        impact = (issue.get('impact') or '').title()
        lines.append('')
        lines.append(f'{i}. {issue.get("title", "")} ({impact} impact)')
        if issue.get('description'):
            lines.append(f'   {issue["description"]}')

    lines += [
        '',
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


def login_page(request):
    # TODO: wire to Django auth in Phase 3 — for now POST redirects to a
    # "portal coming soon" page so the form is testable end-to-end.
    if request.method == 'POST':
        return redirect('public:portal_coming_soon')
    return render(request, 'public/login.html', {
        'active_nav': 'login',
        'meta_title': 'Client Login — Aspired Websites',
        'meta_description': 'Client portal login for Aspired Websites projects.',
    })


def portal_coming_soon(request):
    return render(request, 'public/portal_coming_soon.html', {
        'active_nav': 'login',
        'meta_title': 'Portal Coming Soon — Aspired Websites',
    })
