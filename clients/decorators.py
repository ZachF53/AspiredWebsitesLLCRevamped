"""Access-control decorators for the client portal."""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect

from .models import ClientProfile
from .portal_resolvers import (
    resolve_account_for_user,
    resolve_website,
)


def client_required(view_func):
    """
    Gate a portal view: the user must be authenticated AND have a
    ClientProfile. Anyone else is bounced to /login/ with a ?next= back here.

    On success the following are attached to the request:

      request.client_profile  — legacy single profile (unchanged).
      request.account         — Account (Phase C resolver, derived
                                from the profile during the transition).
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
        profile = ClientProfile.objects.filter(user=request.user).first()
        if profile is None:
            return redirect_to_login(request.get_full_path())
        request.client_profile = profile

        # ── Phase C — resolve Account + Website ──
        # Account: 1:1 with User (after Phase B backfill).
        request.account = resolve_account_for_user(request.user)
        # Website: from URL kwarg (when mounted under /portal/site/<slug>/),
        # else session, else the account's sole website if exactly one.
        # Per-website views consume `request.website`; account-wide views
        # ignore it. The slug kwarg is consumed here so the wrapped
        # view doesn't have to declare it.
        slug_kwarg = kwargs.pop('website_slug', None)
        request.website = resolve_website(
            request, request.account, slug_from_url=slug_kwarg)

        # Onboarding gate.
        status = getattr(profile, 'onboarding_status', 'onboarding_complete')

        if status == 'pending_setup':
            # Shouldn't happen — the user shouldn't have a password until
            # they've consumed the token — but if it does, send them to
            # finish setup rather than into a half-broken portal.
            token = getattr(profile, 'onboarding_token', None)
            if token and not token.used:
                return redirect(token.get_setup_url())
            # No usable token → bail to login so an admin can rebuild.
            return redirect_to_login(request.get_full_path())

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
