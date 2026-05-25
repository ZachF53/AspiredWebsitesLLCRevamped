"""Domain-related transactional emails."""

import logging

from clients.emails import send_branded

logger = logging.getLogger(__name__)


def _first_name(client):
    raw = (client.contact_name or client.firm_name or '').strip()
    return raw.split(' ')[0] if raw else 'there'


def send_registered_email(registration):
    """Sent immediately after a successful registration."""
    client = registration.client
    portal_url = 'https://aspiredwebsites.com/portal/domains/'
    text_body = (
        f'Hi {_first_name(client)},\n\n'
        f'{registration.domain_name} is registered to your account.\n\n'
        f'  • Status: Active\n'
        f'  • WHOIS privacy: On\n'
        f'  • Renews: {registration.expires_at.strftime("%B %d, %Y") if registration.expires_at else "in 365 days"}\n\n'
        f'You can manage DNS records, view your renewal, or cancel any '
        f'time from your portal:\n{portal_url}\n\n'
        f'We\'ve already pointed this domain at your website — '
        f'propagation usually takes 5-15 minutes.\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    send_branded(
        subject=f'Your domain {registration.domain_name} is registered',
        template='domain_registered',
        context={
            'first_name': _first_name(client),
            'domain': registration.domain_name,
            'expires_at': registration.expires_at,
            'portal_url': portal_url,
            'preheader': f'{registration.domain_name} is yours — managed from your portal.',
        },
        recipient_list=[client.user.email],
        text_body=text_body,
        secure=True,
    )


def send_renewal_soon_email(registration, days_until):
    """~7 days before renewal — light reminder."""
    client = registration.client
    portal_url = 'https://aspiredwebsites.com/portal/domains/'
    text_body = (
        f'Hi {_first_name(client)},\n\n'
        f'Quick heads-up: {registration.domain_name} renews in '
        f'{days_until} days. We\'ll auto-charge your card on file — '
        f'nothing for you to do.\n\n'
        f'If you\'d like to cancel before renewal, just head to your '
        f'portal:\n{portal_url}\n\n'
        f'— Aspired Websites LLC\n'
    )
    send_branded(
        subject=f'{registration.domain_name} renews in {days_until} days',
        template='domain_renewal_soon',
        context={
            'first_name': _first_name(client),
            'domain': registration.domain_name,
            'days_until': days_until,
            'expires_at': registration.expires_at,
            'portal_url': portal_url,
            'preheader': 'Automatic renewal coming up.',
        },
        recipient_list=[client.user.email],
        text_body=text_body,
        secure=True,
    )


def send_renewal_succeeded_email(registration):
    """Sent after a successful annual renewal."""
    client = registration.client
    portal_url = 'https://aspiredwebsites.com/portal/domains/'
    text_body = (
        f'Hi {_first_name(client)},\n\n'
        f'{registration.domain_name} renewed for another year. Your '
        f'next renewal is on {registration.expires_at.strftime("%B %d, %Y") if registration.expires_at else "the same date next year"}.\n\n'
        f'Manage at: {portal_url}\n\n'
        f'— Aspired Websites LLC\n'
    )
    send_branded(
        subject=f'{registration.domain_name} renewed for another year',
        template='domain_renewal_succeeded',
        context={
            'first_name': _first_name(client),
            'domain': registration.domain_name,
            'expires_at': registration.expires_at,
            'portal_url': portal_url,
            'preheader': 'Renewed and good for another 12 months.',
        },
        recipient_list=[client.user.email],
        text_body=text_body,
        secure=True,
    )


def send_renewal_failed_email(registration):
    """
    Sent when the Stripe charge SUCCEEDED but the Namecheap renew
    call failed (rare, but real). Tells the client we're on it
    while admin resolves manually.
    """
    client = registration.client
    text_body = (
        f'Hi {_first_name(client)},\n\n'
        f'We hit a snag renewing {registration.domain_name} with '
        f'the registrar — we\'re sorting it out manually now. Your '
        f'domain stays active during this. You don\'t need to do '
        f'anything; we\'ll email when it\'s resolved.\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    send_branded(
        subject=f'Heads-up: working on a renewal hiccup for {registration.domain_name}',
        template='domain_renewal_failed',
        context={
            'first_name': _first_name(client),
            'domain': registration.domain_name,
            'preheader': 'Domain stays active — we\'re handling it.',
        },
        recipient_list=[client.user.email],
        text_body=text_body,
        secure=True,
    )


def send_transfer_out_email(registration, epp_code):
    """
    Cancel-out email with the EPP/auth code + transfer instructions.

    `epp_code` may be empty if Namecheap is gating it behind an
    email-to-registrant verification — in that case the email tells
    the client to watch for the separate Namecheap-issued email.
    """
    client = registration.client
    text_body = (
        f'Hi {_first_name(client)},\n\n'
        f'You cancelled {registration.domain_name}. Your service '
        f'continues until '
        f'{registration.expires_at.strftime("%B %d, %Y") if registration.expires_at else "the end of your current cycle"}; '
        f'after that the domain expires unless you transfer it to '
        f'another registrar first.\n\n'
    )
    if epp_code:
        text_body += (
            f'Your transfer auth (EPP) code:\n'
            f'  {epp_code}\n\n'
            f'How to transfer:\n'
            f'  1. Sign up at any registrar (Namecheap, GoDaddy, '
            f'Cloudflare, etc.)\n'
            f'  2. Find their "Transfer in a domain" flow\n'
            f'  3. Enter {registration.domain_name} + paste the auth '
            f'code above\n'
            f'  4. Confirm the transfer email from the registry — '
            f'transfers usually complete in 5-7 days\n\n'
        )
    else:
        text_body += (
            f'Your transfer auth code will arrive in a separate '
            f'email from the registry within 24 hours — keep an eye '
            f'on your inbox.\n\n'
        )
    text_body += (
        f'Questions? Just reply.\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    send_branded(
        subject=f'Transfer-out package for {registration.domain_name}',
        template='domain_transfer_out',
        context={
            'first_name': _first_name(client),
            'domain': registration.domain_name,
            'epp_code': epp_code,
            'expires_at': registration.expires_at,
            'preheader': 'Auth code + transfer instructions inside.',
        },
        recipient_list=[client.user.email],
        text_body=text_body,
        secure=True,
    )
