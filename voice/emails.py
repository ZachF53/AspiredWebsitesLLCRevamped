"""
Usage warnings: 80% and 100% of the monthly talk-minute allowance.

Both are transactional, so they use the branded HTML path
(``clients.emails.send_branded``) rather than the plain-text cold-outreach
style.

## Tone, and why it is not an alarm

The owner chose "keep answering and bill the overage" over a hard cap, so
these emails are not a warning that something is about to break. Nothing
breaks. The receptionist keeps answering every call. What changes is that
minutes past the allowance cost 25 cents each, and the only unacceptable
outcome is an invoice that arrives unexplained.

So the 80% email is informational and the 100% email states the rate and
the running total. Neither asks the client to do anything, because there
is nothing they need to do — and a "take action" framing on a product
that is working correctly would read as a billing trap.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _recipient(program):
    """Where to send. Support email if given, else the account's login."""
    if program.support_email:
        return program.support_email
    user = getattr(program.account, 'user', None)
    return getattr(user, 'email', '') or ''


def _first_name(program):
    name = (program.account.contact_name or '').strip()
    return name.split()[0] if name else 'there'


def send_usage_warning(period, *, level):
    """Email the client that they have hit `level`% of their minutes.

    Returns True if a message was handed to the mail backend. Never
    raises — the caller has already stamped the warned_* timestamp, so a
    mail failure must not cause a re-send on the next call.
    """
    program = period.program
    to = _recipient(program)
    if not to:
        logger.warning(
            'voice.emails: no recipient for program %s; skipping %s%% '
            'warning', program.pk, level)
        return False

    rate = period.overage_rate
    # Built by hand rather than with '%-d'/'%#d': those are
    # platform-specific (glibc vs MSVC) and this runs on Windows in dev
    # and Linux in prod, so either spelling breaks one of them.
    period_end_label = (f'{period.period_end:%B} '
                        f'{period.period_end.day}')
    context = {
        'first_name': _first_name(program),
        'business_name': program.display_name,
        'level': level,
        'minutes_used': period.minutes_used,
        'included_minutes': period.included_minutes,
        'minutes_remaining': period.minutes_remaining,
        'percent_used': period.percent_used,
        'overage_minutes': period.overage_minutes,
        'overage_amount': period.overage_amount,
        'rate_cents': int(rate * 100),
        'period_end': period.period_end,
        'period_end_label': period_end_label,
        'portal_url': f'{settings.SITE_BASE_URL}/portal/voice/',
        'is_over': level >= 100,
    }

    if level >= 100:
        subject = (f'Your receptionist has used all '
                   f'{period.included_minutes} minutes this month')
        text_body = (
            f"Hi {context['first_name']},\n\n"
            f"Your AI receptionist has now used all "
            f"{period.included_minutes} of this month's included talk "
            f"minutes.\n\n"
            f"Nothing stops. It keeps answering every call exactly as "
            f"before. Minutes from here until "
            f"{period_end_label} are billed at "
            f"{context['rate_cents']} cents each, and they will appear "
            f"as one line on your next invoice.\n\n"
            f"So far that is {period.overage_minutes} extra minutes "
            f"(${period.overage_amount}).\n\n"
            f"You can see every call at {context['portal_url']}\n\n"
            f"If this is becoming a normal month for you, reply and we "
            f"will talk about a larger allowance that works out cheaper "
            f"than the per-minute rate.\n\n"
            f"Zachery Long\nAspired Websites LLC\n"
        )
    else:
        subject = (f"Your receptionist has used {period.percent_used}% of "
                   f"this month's minutes")
        text_body = (
            f"Hi {context['first_name']},\n\n"
            f"Quick heads up: your AI receptionist has used "
            f"{period.minutes_used} of its {period.included_minutes} "
            f"included talk minutes this month, with "
            f"{period.minutes_remaining} left until "
            f"{period_end_label}.\n\n"
            f"No action needed and nothing changes. If you do go past "
            f"the allowance the receptionist keeps answering, and the "
            f"extra minutes are billed at {context['rate_cents']} cents "
            f"each on your next invoice.\n\n"
            f"You can see every call it has taken at "
            f"{context['portal_url']}\n\n"
            f"Zachery Long\nAspired Websites LLC\n"
        )

    try:
        from clients.emails import send_branded
        send_branded(
            subject=subject,
            template='voice_usage_warning',
            context=context,
            recipient_list=[to],
            text_body=text_body,
        )
        return True
    except Exception:
        logger.exception(
            'voice.emails: %s%% warning failed for program %s',
            level, program.pk)
        return False
