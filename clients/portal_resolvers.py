"""
Portal request-resolvers — Phase C1.

Bridges the legacy ``request.client_profile`` (single ClientProfile per
user) to the post-refactor Account + Website model so existing views
keep working while new account/website-aware code can read
``request.account`` and ``request.website`` directly.

Resolution order:

  request.account   — Account.objects.get(user=request.user) if the
                      backfill has run; falls back to a fresh
                      ``Account`` derived from the legacy
                      ClientProfile so unmigrated environments still
                      render.

  request.website   — resolved from (in order):
                        1. ``website_slug`` URL kwarg (when mounted
                           under /portal/site/<slug>/)
                        2. session['active_website_slug']
                        3. the account's sole Website if exactly one
                        4. None (caller is expected to redirect to
                           the chooser).

  request.client_profile — preserved exactly as before, so every
                      existing view + template that reads it keeps
                      working unchanged.

Session storage:

  active_website_slug — set by the chooser on selection; cleared on
                      logout. Persists across same-session
                      navigations so the user doesn't re-pick on
                      every page.
"""

from django.shortcuts import redirect
from django.urls import reverse


SESSION_KEY_ACTIVE_WEBSITE = 'active_website_slug'


def resolve_account_for_user(user):
    """
    Return the Account for a logged-in user. Prefers the post-refactor
    Account row (1:1 with User); falls back to deriving from the
    legacy ClientProfile so pre-Phase-B environments still serve
    requests.
    """
    if user is None or not user.is_authenticated:
        return None
    # Lazy imports — this module is imported by decorators that load
    # early in the request cycle; the model registry must be ready.
    from clients.account_models import Account
    from clients.models import ClientProfile

    acc = Account.objects.filter(user=user).first()
    if acc:
        return acc
    # Fallback path — legacy environment where the backfill hasn't run.
    profile = ClientProfile.objects.filter(user=user).first()
    if profile:
        return Account.objects.filter(legacy_client_profile=profile).first()
    return None


def resolve_website(request, account, *, slug_from_url=None):
    """
    Pick the Website this request is operating on. The resolved value
    is the source of truth for templates rendering URLs, copy, etc.

    Order:
      1. ``slug_from_url`` (URL kwarg from /portal/site/<slug>/) —
         always wins, and is also written into the session so the
         user's choice "sticks" across plain /portal/... URLs.
      2. Session ``active_website_slug`` — set by the chooser.
      3. Account's only Website (auto-pick when there's only one,
         since a chooser screen would be a useless extra click).
      4. None (caller redirects to the chooser).
    """
    if account is None:
        return None
    qs = account.websites.all()

    if slug_from_url:
        ws = qs.filter(slug=slug_from_url).first()
        if ws is not None:
            request.session[SESSION_KEY_ACTIVE_WEBSITE] = ws.slug
            return ws
        # Slug in URL but no match for this account — let the caller
        # 404. Returning None would silently fall through to an
        # unrelated website which would be a privacy break.
        return None

    stored = request.session.get(SESSION_KEY_ACTIVE_WEBSITE)
    if stored:
        ws = qs.filter(slug=stored).first()
        if ws is not None:
            return ws
        # Stale stored slug (deleted website, reassigned, etc.) —
        # clear it so we don't keep round-tripping.
        request.session.pop(SESSION_KEY_ACTIVE_WEBSITE, None)

    if qs.count() == 1:
        only = qs.first()
        # Persist for subsequent requests — same effect as the chooser
        # would have, but invisible to the user.
        request.session[SESSION_KEY_ACTIVE_WEBSITE] = only.slug
        return only

    return None


def clear_active_website(request):
    """Drop the session-stored website (called from logout)."""
    request.session.pop(SESSION_KEY_ACTIVE_WEBSITE, None)


def chooser_url():
    """The chooser endpoint — kept as a helper so the URL name lives
    in exactly one place."""
    return reverse('clients:chooser')


def redirect_to_chooser():
    """Convenience used by decorators that decide they need a pick."""
    return redirect('clients:chooser')
