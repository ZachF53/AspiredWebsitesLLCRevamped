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

from .password_models import PasswordSetupToken

logger = logging.getLogger(__name__)


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
        if not password:
            error = 'Please choose a password.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            try:
                validate_password(password, pst.user)
            except ValidationError as e:
                error = ' · '.join(e.messages)
        if not error:
            pst.user.set_password(password)
            pst.user.save(update_fields=['password'])
            pst.consumed_at = timezone.now()
            pst.save(update_fields=['consumed_at'])
            login(request, pst.user)
            # Redirect to onboarding dispatch — they have one in progress
            from django.shortcuts import redirect
            return redirect(reverse('onboarding:dispatch'))

    return render(request, 'onboarding/set_password.html', {
        'token': pst.token,
        'email': pst.user.email,
        'error': error,
        'password_help': password_validators_help_texts(),
    })


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
    send_mail(
        subject=subject,
        message=body,
        from_email=getattr(
            settings, 'DEFAULT_FROM_EMAIL',
            'zachery@aspiredwebsites.com'),
        recipient_list=[user.email],
        fail_silently=False,
    )
