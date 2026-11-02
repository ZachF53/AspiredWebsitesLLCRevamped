"""
Our own pre-charge reminder email, and the daily sweep that sends it.

## Why we send this instead of letting Stripe do it

A trial that converts to a paid subscription needs a reminder before the
first charge. Stripe will send one (Settings → Billing → "Send emails
about upcoming trial expirations"), and the first cut of this work
relied on that checkbox.

Sending it ourselves is better on every axis that matters here. It comes
from the address the customer already corresponds with, in the same
branded template as every other email this business sends, so it does
not read like a third-party billing notice from a company they have
never heard of. It carries OUR cancel link, which lands on
aspiredwebsites.com rather than a Stripe-hosted page. And the wording is
ours, so it can say "if it is working for you, do nothing" instead of a
generic expiry warning.

It also removes a hand step that was easy to forget and invisible when
missed: nothing in the application could tell whether that Stripe
checkbox was ticked, and the failure mode was silent.

## Why a daily sweep and not a scheduled task per signup

Scheduling a Celery task eleven days out is not safe on this project.
Redis-backed countdowns of that length do not survive a worker restart
or a deploy, and the reference implementation for "do something N days
later" here is billing/dunning.py: a daily beat job that asks which rows
are due now. Same pattern, same reason.

The sweep is also idempotent by construction, which a countdown is not:
it selects on `reminder_sent_at IS NULL`, so a double run, a retried
beat tick or a manual invocation cannot mail anybody twice.

## What it deliberately does not do

It does not cancel anything, charge anything, or touch Stripe at all. It
reads `trial_end` off our own rows (stored at signup) and sends mail.
A sweep that could mutate billing state would be a far more frightening
thing to run from cron.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def reminder_days():
    """How many days before the charge the reminder goes out.

    Three by default, matching when Stripe would have fired
    `trial_will_end`, so the GHL `trial-ending-*` tag and our email land
    together rather than days apart.
    """
    try:
        days = int(getattr(settings, 'TRIAL_REMINDER_DAYS', 3))
    except (TypeError, ValueError):
        days = 3
    return max(1, days)


def manage_url_for(acceptance):
    """Absolute URL of the customer-facing manage/cancel page.

    Absolute because it goes in an email. Built from SITE_BASE_URL
    rather than a request, since the sweep has no request.
    """
    from django.urls import reverse

    base = (getattr(settings, 'SITE_BASE_URL', '')
            or 'https://aspiredwebsites.com').rstrip('/')
    path = reverse('billing:trial_manage',
                   kwargs={'tier_slug': acceptance.product_slug})
    return f'{base}{path}?ref={acceptance.pk}'


def _first_name_for(acceptance):
    """Best available first name, falling back to "there".

    Checkout collects only an email, so the name comes from the Account
    provisioning created. "Hi there," is a worse greeting than "Hi
    Dave," and a much better one than "Hi ,".
    """
    try:
        from clients.account_models import Account
        account = (Account.objects
                   .filter(user__email__iexact=acceptance.email)
                   .first())
        if account is not None:
            name = (account.contact_name or '').strip()
            if name:
                return name.split()[0]
    except Exception:  # noqa: BLE001
        logger.exception('trial reminder: name lookup failed for %s',
                         acceptance.email)
    return 'there'


def send_trial_ending_email(acceptance, *, now=None):
    """Send the pre-charge reminder. Returns True if it went out.

    Stamps `reminder_sent_at` only on success, so a send failure leaves
    the row due and the next daily pass retries it. That ordering is the
    point: the reminder is a compliance obligation, and silently losing
    one because a stamp landed before the send is exactly the failure
    worth engineering against.
    """
    from billing.pricing_models import ServiceTier
    from clients.emails import send_branded

    now = now or timezone.now()
    tier = ServiceTier.objects.filter(slug=acceptance.product_slug).first()
    product_name = tier.name if tier else 'your subscription'
    # Price comes from the tier, like every other price on this site.
    # The customer is billed whatever Stripe Price their subscription
    # was created on, which is this tier's price at signup; for the
    # receptionist that is a cohort rung, so never hardcode it.
    price_display = tier.get_price_display() if tier else ''

    if acceptance.trial_end is None:
        logger.warning(
            'trial reminder: no trial_end on acceptance %s, skipping',
            acceptance.pk)
        return False

    # LOCALISED ONCE, and the localised value is what goes to the
    # template too. This is not a tidiness point.
    #
    # trial_end is stored in UTC. Django's `|date:` filter localises an
    # aware datetime automatically, but an f-string does not. So
    # formatting the raw value here while the HTML template formatted
    # the same field put two DIFFERENT charge dates in one email
    # whenever UTC and America/New_York disagreed about the date, which
    # is every evening after 7pm Eastern. The subject line and the
    # plain-text body said one day, the HTML said the day before.
    #
    # On a pre-charge disclosure that contradiction is a dispute waiting
    # to happen, so both halves now read from the same localised
    # datetime.
    trial_end = timezone.localtime(acceptance.trial_end)
    local_now = timezone.localtime(now)

    days_left = max(0, (trial_end.date() - local_now.date()).days)
    first_name = _first_name_for(acceptance)
    manage_url = manage_url_for(acceptance)
    support_phone = '(210) 201-0375'

    # Built by hand rather than with a %-d / %#d strftime flag: the
    # no-pad day directive differs between platforms (%-d on Linux,
    # %#d on Windows) and this code runs on both.
    end_long = f'{trial_end:%B} {trial_end.day}, {trial_end.year}'
    end_short = f'{trial_end:%B} {trial_end.day}'

    text_body = (
        f'Hi {first_name},\n\n'
        f'Your free trial of {product_name} ends on {end_long}.\n\n'
        f'On that date your card is charged {price_display}, and then '
        f'{price_display} on the same date each month. Nothing has been '
        f'charged so far.\n\n'
        f'If it is working for you, do nothing.\n\n'
        f'If you would rather stop, cancel before then and you will not be '
        f'charged anything at all:\n{manage_url}\n\n'
        f'Questions? Reply to this email, or call {support_phone}.\n\n'
        f'- Aspired Websites LLC\n'
    )

    send_branded(
        subject=f'Your {product_name} trial ends {end_short}',
        template='trial_ending',
        context={
            'first_name': first_name,
            'product_name': product_name,
            'price_display': price_display,
            'trial_end': trial_end,
            'days_left': days_left,
            'manage_url': manage_url,
            'support_phone': support_phone,
            'preheader': (f'{price_display} on {end_short}. '
                          f'Cancel before then and you pay nothing.'),
        },
        recipient_list=[acceptance.email],
        text_body=text_body,
        # The manage URL carries the acceptance ref. secure=True stops
        # SendGrid rewriting it for click tracking, which would both
        # break the link's meaning and put the token on a third-party
        # redirector.
        secure=True,
        # Not silent: the sweep needs to know, so a failure leaves the
        # row due for tomorrow instead of being stamped as sent.
        fail_silently=False,
    )

    acceptance.reminder_sent_at = now
    acceptance.save(update_fields=['reminder_sent_at', 'updated_at'])
    logger.info('trial reminder: sent to %s for %s (trial_end %s)',
                acceptance.email, acceptance.product_slug, trial_end)
    return True


def due_reminders(*, now=None):
    """Open trials whose reminder is due. Ordered oldest trial first.

    "Due" is trial_end within the reminder window and still in the
    future. The upper bound is what makes this a reminder rather than a
    notification: mailing someone the day AFTER they were charged is
    worse than not mailing them.
    """
    from datetime import timedelta

    from billing.terms_models import TermsAcceptance

    now = now or timezone.now()
    window_end = now + timedelta(days=reminder_days())

    return (TermsAcceptance.objects
            .filter(reminder_sent_at__isnull=True,
                    cancelled_at__isnull=True,
                    converted_at__isnull=True,
                    trial_end__isnull=False,
                    trial_end__gt=now,
                    trial_end__lte=window_end)
            .exclude(stripe_subscription_id='')
            .order_by('trial_end'))


def run_trial_reminder_sweep(now=None):
    """Send every due reminder. Returns (sent, failed).

    One row's failure must not stop the rest: these are independent
    customers and independent obligations, so each send is isolated.
    """
    now = now or timezone.now()
    sent = 0
    failed = 0
    for acceptance in due_reminders(now=now):
        try:
            if send_trial_ending_email(acceptance, now=now):
                sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception(
                'trial reminder: send failed for %s (acceptance %s), will '
                'retry on the next sweep', acceptance.email, acceptance.pk)
    if sent or failed:
        logger.info('trial reminder sweep: %s sent, %s failed', sent, failed)
    return sent, failed
