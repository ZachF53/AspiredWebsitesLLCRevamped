"""Transactional emails for the client portal (sent via SendGrid SMTP)."""

import json

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.template.loader import render_to_string


# SendGrid SMTP click-tracking-off / open-tracking-off header value.
# Used for security-sensitive emails (account setup links, password
# reset tokens, etc.) so SendGrid doesn't rewrite their URLs through
# url6271.aspiredwebsites.com — a custom link-branding subdomain
# whose SSL cert hasn't been enabled in the SendGrid UI, causing a
# browser SSL error when the client clicks through.
#
# SendGrid honours `X-SMTPAPI` on SMTP-sent mail. The JSON below
# disables both HTML and plain-text click tracking plus open
# tracking, so every URL in the body is left exactly as written.
_NO_TRACKING_HEADER = json.dumps({
    'filters': {
        'clicktrack': {
            'settings': {'enable': 0, 'enable_text': 0},
        },
        'opentrack': {
            'settings': {'enable': 0},
        },
    },
})


def send_secure_mail(*, subject, message, from_email, recipient_list,
                     fail_silently=True):
    """
    `django.core.mail.send_mail` shaped, but adds X-SMTPAPI so SendGrid
    leaves every URL in the body verbatim — no click-tracking rewrite.

    Use for any email containing a one-time token URL (account setup,
    password reset, magic-link login). Use the regular `send_mail`
    for everything else so SendGrid analytics keep working.

    Routes through `EmailMessage` rather than the `send_mail` helper
    because `send_mail` doesn't expose headers — `EmailMessage`
    does, via the `headers=` kwarg. The legal-address footer is
    still applied (handled by `AspiredEmailBackend.send_messages`
    on every outgoing message).
    """
    msg = EmailMessage(
        subject=subject,
        body=message,
        from_email=from_email,
        to=recipient_list,
        headers={'X-SMTPAPI': _NO_TRACKING_HEADER},
    )
    msg.send(fail_silently=fail_silently)


def send_branded(*, subject, template, context, recipient_list,
                 text_body, from_email=None, secure=False,
                 attachments=None, fail_silently=True):
    """
    Branded HTML transactional email — multipart/alternative (text +
    HTML) so every client gets a readable version.

    Args:
      subject        — email subject line
      template       — name without .html, resolved as
                       `core/templates/emails/<template>.html`
      context        — dict passed to the template (first_name,
                       setup_url, etc.)
      recipient_list — list of recipient email addresses
      text_body      — plain-text alternative; required, used by
                       clients that can't or won't render HTML, and
                       lifts our spam score
      from_email     — defaults to settings.EMAIL_FROM_MAIN
      secure         — when True, attaches the X-SMTPAPI header so
                       SendGrid leaves every URL verbatim (use for
                       any email containing a one-time token URL)
      attachments    — optional list of (filename, content_bytes,
                       mimetype) tuples
      fail_silently  — passed through to .send()

    The legal address footer is still appended by
    `AspiredEmailBackend.send_messages` automatically.
    """
    from_email = from_email or settings.EMAIL_FROM_MAIN
    html_body = render_to_string(f'emails/{template}.html', context)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=recipient_list,
    )
    msg.attach_alternative(html_body, 'text/html')

    if secure:
        msg.extra_headers['X-SMTPAPI'] = _NO_TRACKING_HEADER
    if attachments:
        for filename, content, mimetype in attachments:
            msg.attach(filename, content, mimetype)

    msg.send(fail_silently=fail_silently)


def _first_name(client):
    """Best-effort first name for personalising emails."""
    raw = (client.contact_name or client.firm_name or '').strip()
    return raw.split(' ')[0] if raw else 'there'


def send_onboarding_setup_email(client, token):
    """
    First touchpoint after invoice payment — emails the setup-link so the
    client can create their password + PIN and unlock the portal. Token
    URL is opaque; the OnboardingToken row authenticates the request.

    SECURITY-SENSITIVE — contains the one-time setup token. `secure=True`
    attaches the X-SMTPAPI header so SendGrid does NOT rewrite the URL
    through url6271.aspiredwebsites.com (no SSL cert there → SSL error).
    """
    name = _first_name(client)
    setup_url = token.get_setup_url()
    text_body = (
        f'Welcome aboard, {name}.\n\n'
        f'Thank you for your payment — your Aspired Websites account is '
        f'ready to be set up.\n\n'
        f'Click the link below to create your password and security PIN:\n\n'
        f'{setup_url}\n\n'
        f'Once your account is set up, you\'ll be asked to complete a '
        f'short intake form so we have everything we need to start '
        f'building your website. Work on your site can\'t begin until '
        f'the intake is submitted.\n\n'
        f'If you have any questions, just reply to this email.\n\n'
        f'— Zachery Long\n'
        f'Aspired Websites LLC\n'
    )
    send_branded(
        subject='Your Aspired Websites account is ready',
        template='onboarding_setup',
        context={
            'first_name': name,
            'setup_url': setup_url,
            'preheader': (
                'Set up your password and PIN to access your portal '
                'and start the intake.'),
        },
        recipient_list=[client.user.email],
        text_body=text_body,
        secure=True,
    )


def send_account_setup_complete_email(client):
    """
    Sent after the client finishes the setup page (password + PIN) —
    nudges them straight into the intake form, the only portal page
    they can reach in `pending_intake` state.
    """
    name = _first_name(client)
    body = (
        f'Hi {name},\n\n'
        f'Your Aspired Websites account has been created successfully.\n\n'
        f'Before we can begin building your website, we need a few '
        f'details from you. Please complete your intake form — it takes '
        f'about 10 minutes and gives us everything we need to build '
        f'your site.\n\n'
        f'Complete your intake form:\n'
        f'https://aspiredwebsites.com/portal/intake/\n\n'
        f'Important: Work on your website cannot begin until your '
        f'intake form is submitted.\n\n'
        f'Once submitted, we\'ll review your information and reach out '
        f'within 1 business day to confirm your project start date.\n\n'
        f'— Zachery Long\n'
        f'Aspired Websites LLC\n'
        f'210-896-2536\n'
    )
    # Contains /portal/intake/ — same domain as the portal login, so
    # technically click tracking would still resolve, but we keep this
    # off too so the whole onboarding flow has zero-rewrite URLs and
    # the client never sees a different domain in any setup email.
    send_secure_mail(
        subject='Your account is ready — one more step before we start',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_intake_received_email(client):
    """
    Sent the moment the intake is submitted — closes the loop on
    onboarding and sets expectations for the next 1 business day.
    """
    name = _first_name(client)
    body = (
        f'Hi {name},\n\n'
        f'Thank you for completing your intake form. We have everything '
        f'we need to start planning your website.\n\n'
        f'What happens next:\n'
        f'  1. We\'ll review your information\n'
        f'  2. Reach out within 1 business day to confirm your project '
        f'start date\n'
        f'  3. Your website build begins\n\n'
        f'You can log into your portal anytime to track progress:\n'
        f'https://aspiredwebsites.com/portal/\n\n'
        f'— Zachery Long\n'
        f'Aspired Websites LLC\n'
    )
    send_mail(
        subject='We\'ve received your intake — we\'ll be in touch',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_contract_ready_email(contract, sign_url):
    """Email the client their contract signing link (staff-triggered)."""
    client = contract.client
    name = client.contact_name or client.firm_name
    body = (
        f'Hi {name},\n\n'
        f'Your website build contract with Aspired Websites is ready to sign.\n\n'
        f'Review and sign it here:\n{sign_url}\n\n'
        f'Once signed, we’ll send your deposit invoice and get started.\n\n'
        f'— Zachery Long\n'
        f'Aspired Websites LLC\n'
    )
    send_mail(
        subject='Your contract is ready to sign — Aspired Websites',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_contract_signed_email(contract):
    """Confirm to the client that their contract was signed."""
    client = contract.client
    name = client.contact_name or client.firm_name
    body = (
        f'Hi {name},\n\n'
        f'Thanks — your website build contract with Aspired Websites is signed.\n\n'
        f'Your deposit invoice is on its way and will arrive shortly in a separate '
        f'email. Your project officially starts the moment your deposit is received.\n\n'
        f'If you have any questions in the meantime, just reply to this email or '
        f'call us at 210-896-2536.\n\n'
        f'— Aspired Websites LLC\n'
    )
    send_mail(
        subject='Your contract is signed — Aspired Websites',
        message=body,
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_welcome_email(client, project):
    """Sent once the deposit clears — project is active, intake unlocked."""
    name = client.contact_name or client.firm_name
    body = (
        f'Hi {name},\n\n'
        f'Your deposit is in — welcome aboard! Your website project is now '
        f'active and we’re getting started.\n\n'
        f'Your next step is to complete your intake form in the client portal '
        f'so we have everything we need to build your site:\n'
        f'https://aspiredwebsites.com/portal/intake/\n\n'
        f'Sign in any time at https://aspiredwebsites.com/login/\n\n'
        f'— Zachery Long\n'
        f'Aspired Websites LLC\n'
    )
    send_mail(
        subject='Welcome to Aspired Websites — your project is active',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_intake_reminder_email(project, day):
    """Day-2 / Day-4 nudge to finish the intake form."""
    client = project.client
    name = client.contact_name or client.firm_name
    body = (
        f'Hi {name},\n\n'
        f'A quick reminder to complete your intake form so we can keep your '
        f'website project moving:\n'
        f'https://aspiredwebsites.com/portal/intake/\n\n'
        f'It only takes a few minutes. If anything is unclear, just reply to '
        f'this email or call 210-896-2536.\n\n'
        f'— Aspired Websites LLC\n'
    )
    send_mail(
        subject='Quick reminder: your intake form',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_payment_failed_email(client, day):
    """Payment-failure dunning email (Day 3 / 7 / 14)."""
    name = client.contact_name or client.firm_name
    body = (
        f'Hi {name},\n\n'
        f'We were unable to process your recent payment to Aspired Websites. '
        f'Please update your payment details to keep your account in good '
        f'standing:\n'
        f'https://aspiredwebsites.com/portal/invoices/\n\n'
        f'If you have any questions, call us at 210-896-2536 and we’ll help '
        f'sort it out.\n\n'
        f'— Aspired Websites LLC\n'
    )
    send_mail(
        subject='Payment issue on your Aspired Websites account',
        message=body,
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[client.user.email],
        fail_silently=True,
    )


def send_maintenance_handoff_email(client, handoff_url, followup_day=None):
    """
    Maintenance handoff email for Moonieful-referred clients. `followup_day`
    is set (3/7/14) for the reminder follow-ups, None for the first send.
    """
    name = client.contact_name or client.firm_name
    if followup_day:
        subject = 'Reminder: set up your website maintenance plan'
        opener = (
            'Following up — your website is live, but you haven’t set up a '
            'maintenance plan yet.'
        )
    else:
        subject = 'Your site is live — set up your maintenance plan'
        opener = 'Your website is live!'
    body = (
        f'Hi {name},\n\n'
        f'{opener}\n\n'
        f'Set up a maintenance plan to keep your site secure, updated, and '
        f'performing:\n{handoff_url}\n\n'
        f'This link is valid for 48 hours.\n\n'
        f'— Aspired Websites LLC\n'
    )
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[client.user.email],
        fail_silently=True,
    )
