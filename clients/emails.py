"""Transactional emails for the client portal (sent via SendGrid SMTP)."""

from django.conf import settings
from django.core.mail import send_mail


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
