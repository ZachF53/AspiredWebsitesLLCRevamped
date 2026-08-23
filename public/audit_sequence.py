"""
Audit follow-up sequence — three emails, tailored to what we measured.

WHY THIS IS NOT COLD OUTREACH
-----------------------------
These people typed their own URL into the audit tool and then handed
over an email address to get the results. They asked. That makes this a
warm, requested sequence, and it changes three things:

  * It sends through SendGrid from zacherylong@aspiredwebsites.com --
    the address they expect, on the domain they just visited. Routing it
    through Instantly's secondary sending domains would be worse: the
    reply-to would not match the site they were just on.
  * The tone assumes context. There is no "I came across your firm";
    they know exactly who we are and why we are writing.
  * It is short. Three emails, then silence.

It IS still commercial email, so CAN-SPAM applies in full: postal
address and a working one-click opt-out on every message. The original
report had neither, which was a gap on the message this business sends
more than any other.

TAILORING
---------
Every email is built from the lead's own worst-scoring category. A site
failing accessibility needs a different conversation from one that is
merely slow, and sending both people the same paragraph throws away the
only thing that makes this sequence worth reading.

WHEN NOTHING IS WRONG
---------------------
A site scoring well everywhere gets the report and then nothing. There
is no follow-up written for "your site is fine", because manufacturing a
problem to justify one is how a useful free tool turns into a funnel
people resent -- and this tool is a genuine lead magnet precisely
because it tells the truth.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Days after the report before each follow-up. Deliberately unhurried:
# these people have a day job, and a nudge two days apart reads as
# pestering when they never asked to be sold to.
FOLLOWUP_1_DAYS = 3
FOLLOWUP_2_DAYS = 8

# Minimum gap between the two follow-ups, enforced on top of
# FOLLOWUP_2_DAYS. Without it, a record whose report sat unsent for ten
# days satisfies both windows at once and receives follow-up 1 and
# follow-up 2 in the same batch, minutes apart. That reads as a broken
# system, which is exactly what it would be.
FOLLOWUP_2_MIN_GAP_DAYS = 4

UNSUBSCRIBE_SALT = 'audit-followup-unsubscribe'

CATEGORY_LABELS = {
    'performance': 'Performance',
    'seo': 'SEO',
    'accessibility': 'Accessibility',
    'best_practices': 'Best Practices',
}

# What a low score in each category COSTS the business. This is the
# tailoring: the same score means something different depending on which
# category it is in, and saying so is the whole value of the follow-up.
CATEGORY_CONSEQUENCE = {
    'performance': (
        'Slow pages lose people before they ever see what you do. '
        'Most visitors give a site a couple of seconds, and on a phone '
        'over mobile data that budget is smaller still.'
    ),
    'seo': (
        'This is the score that decides whether people find you at all. '
        'A site can look excellent and still be close to invisible in '
        'search results.'
    ),
    'accessibility': (
        'Beyond the people it shuts out, this one carries legal weight. '
        'ADA website claims against small businesses have been rising '
        'for years, and law firms are a favoured target precisely '
        'because they should know better.'
    ),
    'best_practices': (
        'This covers the things a visitor never sees but a browser '
        'does -- insecure requests, console errors, outdated libraries. '
        'It is also the closest proxy for how carefully the site was '
        'built.'
    ),
}

# The single most useful thing to fix first, per category. Concrete
# enough to be worth acting on without us, which is the point: an offer
# that only works if you hire us is an advert, not help.
CATEGORY_FIRST_FIX = {
    'performance': (
        'Nine times out of ten the biggest win is images. Most sites '
        'serve photographs several times larger than they display, in '
        'formats from a decade ago. Converting them to WebP and sizing '
        'them properly often halves load time on its own.'
    ),
    'seo': (
        'Start with page titles and meta descriptions. Many sites have '
        'the same title on every page, or the builder default, which '
        'means search engines have nothing to distinguish one page from '
        'another.'
    ),
    'accessibility': (
        'Start with colour contrast and image alt text. They are the '
        'two most common failures, they affect real readers, and both '
        'are fixable without touching how the site looks.'
    ),
    'best_practices': (
        'Start with anything loading over plain http on an otherwise '
        'https page. Browsers increasingly block it outright, which can '
        'silently break a contact form without anyone noticing.'
    ),
}


# ── Unsubscribe ────────────────────────────────────────────────────────

def unsubscribe_token(audit_lead):
    """Signed, unguessable, and tied to this one record.

    No expiry. An opt-out link that stops working is worse than no link:
    the recipient clicks, nothing happens, and their next move is the
    spam button -- which costs the sending domain far more than the
    unsubscribe would have.
    """
    return dumps(audit_lead.pk, salt=UNSUBSCRIBE_SALT)


def resolve_unsubscribe_token(token):
    """Return the AuditLead for a token, or None."""
    from public.models import AuditLead

    try:
        pk = loads(token, salt=UNSUBSCRIBE_SALT)
    except (BadSignature, SignatureExpired):
        return None
    return AuditLead.objects.filter(pk=pk).first()


def _footer(audit_lead):
    """CAN-SPAM footer: postal address plus a working opt-out."""
    path = reverse('public:audit_unsubscribe',
                   args=[unsubscribe_token(audit_lead)])
    site = getattr(settings, 'PRODUCTION_HOST', 'aspiredwebsites.com')
    postal = (getattr(settings, 'COMPANY_POSTAL_ADDRESS', '') or '').strip()
    return (
        '\n\n---\n'
        'Zachery Long, Aspired Websites LLC\n'
        + (postal + '\n' if postal else '')
        + f'Unsubscribe: https://{site}{path}\n'
    )


# ── Copy ───────────────────────────────────────────────────────────────

def _score_line(audit_lead):
    return (
        f'Performance {audit_lead.performance_score}/100  |  '
        f'SEO {audit_lead.seo_score}/100  |  '
        f'Accessibility {audit_lead.accessibility_score}/100  |  '
        f'Best Practices {audit_lead.best_practices_score}/100'
    )


def build_report(audit_lead):
    """Email 1 — the results they asked for, plus the headline finding."""
    key, score = audit_lead.worst_category
    label = CATEGORY_LABELS[key]

    body = [
        f'Here are the full results for {audit_lead.url}.',
        '',
        _score_line(audit_lead),
        '',
    ]

    issues = audit_lead.issues if isinstance(audit_lead.issues, dict) else {}
    for cat in ('performance', 'seo', 'accessibility', 'best_practices'):
        found = issues.get(cat) or []
        if not found:
            continue
        body.append(f'{CATEGORY_LABELS[cat]}:')
        for issue in found[:5]:
            title = (issue.get('title') or '').strip()
            if title:
                body.append(f'  - {title}')
        body.append('')

    if audit_lead.is_healthy:
        body += [
            'Honestly, this is a good result. Nothing here is urgent.',
            '',
            'If you ever want a second opinion on something specific, '
            'reply to this email and I will take a look.',
        ]
    else:
        body += [
            f'The number I would pay attention to is {label}, at '
            f'{score}/100.',
            '',
            CATEGORY_CONSEQUENCE[key],
            '',
            'No action needed from you. I will send one short note in a '
            'few days about what I would fix first, and that is it.',
        ]

    body += ['', '- Zachery Long', 'Aspired Websites LLC']
    return (f'Your website audit: {audit_lead.url}',
            '\n'.join(body) + _footer(audit_lead))


def build_followup_1(audit_lead):
    """Email 2 — the one thing to fix first. Useful without us."""
    key, score = audit_lead.worst_category
    label = CATEGORY_LABELS[key]

    body = [
        f'A few days ago you ran an audit on {audit_lead.url}.',
        '',
        f'Your lowest score was {label} at {score}/100, so here is what '
        f'I would do about it first.',
        '',
        CATEGORY_FIRST_FIX[key],
        '',
        'That is genuinely worth doing whether or not you ever speak to '
        'me. If your developer handles it, forward them this email.',
        '',
        'If you would rather not think about it, I build and maintain '
        'websites for small firms in Texas and Georgia, and my '
        'background is security -- Masters in Cybersecurity and a '
        'CISSP. Reply and I will tell you what it would take.',
        '',
        '- Zachery Long',
        'Aspired Websites LLC',
    ]
    return (f'The first thing I would fix on {audit_lead.url}',
            '\n'.join(body) + _footer(audit_lead))


def build_followup_2(audit_lead):
    """Email 3 — the offer, then silence."""
    key, score = audit_lead.worst_category

    body = [
        f'Last note about {audit_lead.url}, then I will leave you alone.',
        '',
        f'Your audit came back with {CATEGORY_LABELS[key]} at '
        f'{score}/100. If fixing that has moved up your list, here is '
        f'the offer:',
        '',
        'I will do a full security and performance review of the site '
        'and send you the written findings within 48 hours. Free, no '
        'strings, and I will not chase you afterwards. It goes deeper '
        'than the automated audit you already have -- whether your '
        'forms are encrypted end to end, what the site exposes '
        'publicly, and where visitors give up.',
        '',
        'Just reply "yes" and I will get started.',
        '',
        'If not, no problem at all. The audit tool is free and always '
        'will be, so run it again any time something changes.',
        '',
        '- Zachery Long',
        'Aspired Websites LLC',
    ]
    return (f'Anything I can help with on {audit_lead.url}?',
            '\n'.join(body) + _footer(audit_lead))


# ── Sending ────────────────────────────────────────────────────────────

def _may_send(audit_lead):
    """Whether this record may receive anything at all.

    Checks the global SuppressionList as well as the per-record opt-out.
    Somebody who unsubscribed from cold outreach last month has already
    said no; the fact that they arrived through a different door does
    not un-say it.
    """
    from outreach.models import SuppressionList

    if not audit_lead.email:
        return False, 'no email address'
    if audit_lead.unsubscribed:
        return False, 'unsubscribed'
    if SuppressionList.objects.filter(
            email__iexact=audit_lead.email).exists():
        return False, 'on the suppression list'
    return True, ''


def _send(audit_lead, subject, body):
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[audit_lead.email],
        fail_silently=False,
    )


def send_report(audit_lead):
    """Email 1, immediately on email capture."""
    allowed, why = _may_send(audit_lead)
    if not allowed:
        logger.info('audit report not sent (%s): %s', why, audit_lead.pk)
        return False
    subject, body = build_report(audit_lead)
    _send(audit_lead, subject, body)
    audit_lead.report_sent_at = timezone.now()
    audit_lead.save(update_fields=['report_sent_at'])
    return True


def send_followup(audit_lead, number):
    """Email 2 or 3. Returns True when something was sent."""
    allowed, why = _may_send(audit_lead)
    if not allowed:
        logger.info('audit follow-up %s not sent (%s): %s',
                    number, why, audit_lead.pk)
        return False

    # Nothing worth following up about. Said once in the report, then
    # left alone.
    if audit_lead.is_healthy:
        return False

    if number == 1:
        subject, body = build_followup_1(audit_lead)
        field = 'followup_1_sent_at'
    else:
        subject, body = build_followup_2(audit_lead)
        field = 'followup_2_sent_at'

    if getattr(audit_lead, field):
        return False

    _send(audit_lead, subject, body)
    setattr(audit_lead, field, timezone.now())
    audit_lead.save(update_fields=[field])
    return True
