"""Access-control decorators for the client portal."""

import logging
from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect

from .portal_resolvers import (
    resolve_account_for_user,
    resolve_website,
)

logger = logging.getLogger(__name__)


def client_required(view_func):
    """
    Gate a portal view: the user must be authenticated AND be a client —
    which means holding an Account. Anyone else is bounced to /login/
    with a ?next= back here.

    This used to admit a user holding an Account *or* a legacy
    ClientProfile, and attached `request.client_profile` for the ~20
    views that read it. Those views read `request.website` or
    `request.account` now, so the profile is neither looked up nor
    attached — which is what actually severs the portal from the legacy
    table. A name-based scan never saw those twenty reads, because none
    of them mentioned ClientProfile.

    On success the following are attached to the request:

      request.account         — the Account this user owns.
      request.website         — Website the request is scoped to, or
                                None if the caller will redirect to
                                the chooser. Picked from a
                                ``website_slug`` URL kwarg first, then
                                the session, then the account's sole
                                website.

    ── Onboarding gate (Part 5) ──
    Once a profile is loaded, we additionally enforce the onboarding state:

      - `pending_setup`  → bounce to the setup link (token URL). Unlikely
        path — by the time someone is logged in their setup should already
        be done — but covers admin-created edge cases.
      - `pending_intake` → bounce to /portal/intake/. Only the intake form
        itself (and a few utility views marked `allow_pending_intake=True`)
        are reachable until the intake is submitted.
      - `onboarding_complete` → allow through.

    Views that need to be reachable while still pending_intake (intake
    itself, the HTMX intake-save, logout) set
    ``view.allow_pending_intake = True`` after the decorator wraps them —
    see clients/views.py at the bottom of the file.
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        # A maintenance-flow session is scoped to plan selection only — it is
        # not full portal access, so bounce it to a real login.
        if request.session.get('maintenance_flow_only'):
            return redirect_to_login(request.get_full_path())
        # ── Wave 1 — Account is the gate ──
        # Portal access is an account-level fact, so the Account decides
        # admission and the legacy profile is only carried along for the
        # views that still read it. A user with an Account but no profile
        # (the shape every post-cutover signup will have) is let in; before
        # this change they were bounced to the login page in a loop.
        account = resolve_account_for_user(request.user)
        if account is None:
            # Authenticated, but not a client. Bouncing them to the login
            # page loops — login sees a valid session and sends them
            # straight back — so say plainly that this login has no
            # client account rather than cycling forever.
            from django.core.exceptions import PermissionDenied

            logger.warning(
                'portal: user %s is authenticated but owns no Account',
                request.user.pk)
            raise PermissionDenied(
                'This login is not attached to a client account.')

        request.account = account
        # Website: from URL kwarg (when mounted under /portal/site/<slug>/),
        # else session, else the account's sole website if exactly one.
        # Per-website views consume `request.website`; account-wide views
        # ignore it. The slug kwarg is consumed here so the wrapped
        # view doesn't have to declare it.
        slug_kwarg = kwargs.pop('website_slug', None)
        request.website = resolve_website(
            request, request.account, slug_from_url=slug_kwarg)

        # ── Onboarding gate ──
        # The canonical split, finally: account setup (WHOIS contact +
        # vault PIN) is the Account's, the intake form is the Website's.
        # This used to read a single three-state field off the legacy
        # profile, which could not express a client who had finished one
        # build's intake but not another's.
        if account.onboarding_status == 'pending_setup':
            status = 'pending_setup'
        elif (request.website is not None
                and request.website.onboarding_status == 'pending_intake'):
            status = 'pending_intake'
        else:
            status = 'onboarding_complete'

        if status == 'pending_setup':
            # Shouldn't happen — the user shouldn't have a password until
            # they've consumed the token — but if it does, send them to
            # finish setup rather than into a half-broken portal.
            token = getattr(account, 'onboarding_token_new', None)
            if token and not token.used:
                return redirect(token.get_setup_url())

            # No usable token. This used to bail to the login page, which
            # for an ALREADY-AUTHENTICATED user is an infinite redirect
            # loop: login sees a valid session and sends them straight
            # back here. Staging hit it the moment the gate moved onto
            # Account.onboarding_status, because the autocreate signal
            # had stamped 'pending_setup' on a row whose client was long
            # since set up — the staleness clients/account_setup.py
            # warned about.
            #
            # They are authenticated and they own this account, so let
            # them in. A stale flag is not a reason to lock a paying
            # client out of their own portal; it is a reason to tell an
            # admin the flag is wrong.
            logger.error(
                'portal: account %s is pending_setup with no usable '
                'token — admitting the authenticated owner anyway',
                account.pk)
            try:
                from core.system_alerts import record_alert
                record_alert(
                    severity='error',
                    source='clients.portal.stale_pending_setup',
                    message=(f'Account {account.pk} says pending_setup '
                             'but has no usable onboarding token'),
                    detail=('The owner is logged in and was admitted, '
                            'because bouncing an authenticated user to '
                            'the login page loops forever. Fix the '
                            'status, or issue a fresh setup token.'),
                )
            except Exception:
                logger.exception('could not record the stale-setup alert')
            status = 'onboarding_complete'

        if status == 'pending_intake':
            # Allow only views that explicitly opt in (intake itself,
            # intake_save HTMX endpoint, and any future utility view
            # like logout that should work pre-intake).
            if not getattr(view_func, 'allow_pending_intake', False):
                messages.info(
                    request,
                    'Please complete your intake form to access your '
                    'portal. Work on your website cannot begin until '
                    'this is submitted.',
                )
                return redirect('clients:intake')

        return view_func(request, *args, **kwargs)

    return _wrapped


def allow_pending_intake(view_func):
    """
    Marker that lets a `client_required`-wrapped view stay reachable
    while the client is still in the `pending_intake` state.

    Set on the inner view function so the wrapping order doesn't matter:
        @client_required
        @allow_pending_intake
        def intake(request): ...
    """
    view_func.allow_pending_intake = True
    return view_func
