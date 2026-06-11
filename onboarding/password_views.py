"""Magic-link password-setup views."""

import logging

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.password_validation import (
    password_validators_help_texts,
    validate_password,
)
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django_ratelimit.decorators import ratelimit

from .password_models import PasswordSetupToken

logger = logging.getLogger(__name__)


# Token is already UUID-strong, but rate-limit anyway as defense in
# depth against credential-stuffing attacks that spray random tokens.
@ratelimit(key='ip', rate='10/h', method='POST', block=True)
def set_password(request, token):
    """Magic-link landing page. GET shows the form; POST sets the password."""
    pst = get_object_or_404(PasswordSetupToken, token=token)
    if not pst.is_valid:
        return render(request, 'onboarding/set_password_invalid.html', {
            'reason': 'expired' if pst.consumed_at is None else 'consumed',
        })

    error = ''
    if request.method == 'POST':
        password = (request.POST.get('password') or '')
        confirm = (request.POST.get('confirm_password') or '')
        # The 4-digit vault PIN is required for EVERY client — it gates
        # their portal credentials vault, so no account skips it.
        pin = (request.POST.get('pin') or '').strip()
        pin_confirm = (request.POST.get('pin_confirm') or '').strip()

        if not password:
            error = 'Please choose a password.'
        elif password != confirm:
            error = 'Passwords do not match.'
        elif not (pin.isdigit() and len(pin) == 4):
            error = 'Your PIN must be exactly 4 digits.'
        elif pin != pin_confirm:
            error = 'Your PINs do not match.'
        else:
            try:
                validate_password(password, pst.user)
            except ValidationError as e:
                error = ' · '.join(e.messages)

        if not error:
            pst.user.set_password(password)
            pst.user.is_active = True
            pst.user.save(update_fields=['password', 'is_active'])

            # Set the client portal/vault PIN (same crypto path as the
            # build-client setup flow — vault.crypto.hash_client_pin).
            _set_client_pin(pst.user, pin)

            pst.consumed_at = timezone.now()
            pst.save(update_fields=['consumed_at'])
            login(request, pst.user,
                  backend='django.contrib.auth.backends.ModelBackend')
            # Drop them straight into their dashboard — no walkthrough
            # gate, no login screen. The onboarding checklist surfaces
            # inside the portal.
            from django.shortcuts import redirect
            return redirect('clients:dashboard')

    return render(request, 'onboarding/set_password.html', {
        'token': pst.token,
        'email': pst.user.email,
        'error': error,
        'password_help': password_validators_help_texts(),
    })


def _set_client_pin(user, pin):
    """Persist the 4-digit portal PIN on the user's ClientProfile."""
    from vault.crypto import generate_salt, hash_client_pin
    from clients.models import ClientProfile

    cp = ClientProfile.objects.filter(user=user).first()
    if cp is None:
        logger.warning(
            'set_password: no ClientProfile for %s — PIN not stored', user.pk)
        return
    salt = generate_salt()
    cp.client_pin_salt = salt
    cp.client_pin_hash = hash_client_pin(pin, salt)
    cp.client_pin_set = True
    cp.client_pin_failed_attempts = 0
    cp.client_pin_lockout_until = None
    cp.save(update_fields=[
        'client_pin_salt', 'client_pin_hash', 'client_pin_set',
        'client_pin_failed_attempts', 'client_pin_lockout_until',
        'updated_at',
    ])


def send_password_setup_email(user, token=None):
    """Compose + send the welcome / set-password email."""
    if token is None:
        token = PasswordSetupToken.create_for(user)
    base_url = getattr(
        settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
    link = f'{base_url}/set-password/{token.token}/'
    subject = 'Welcome to Aspired Websites — set your password'
    body = (
        f'Hi,\n\n'
        f'Thanks for your purchase! Click below to set your password '
        f'and start your setup walkthrough. The link expires in 7 days.\n\n'
        f'{link}\n\n'
        f'Once your password is set, you will be guided through a short '
        f'onboarding so we can get to work right away.\n\n'
        f'— Zachery'
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(
                settings, 'DEFAULT_FROM_EMAIL',
                'zachery@aspiredwebsites.com'),
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info(
            'password setup email sent to %s for user %s',
            user.email, user.pk)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'password setup email FAILED for %s', user.email)
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='error',
                source='onboarding.password_setup_email',
                message=f'Password setup email failed for {user.email}',
                detail=str(exc)[:2000],
            )
        except Exception:
            pass
        raise
