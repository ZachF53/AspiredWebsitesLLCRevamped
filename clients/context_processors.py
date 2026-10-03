"""
Client-portal context processors.

``portal_services`` exposes a dict describing which service models
the current user's Account has — drives the dynamic sidebar nav
introduced in Phase D4.

Returns:
    {
        'has_website':       bool,
        'has_maintenance':   bool,
        'has_social_media':  bool,
        'has_droplet':       bool,
    }
"""


def portal_services(request):
    """Inspect the user's Account; return per-service booleans plus the
    per-service onboarding ("intake") link info the sidebar renders under
    each service group."""
    blank = {
        'has_website': False,
        'has_maintenance': False,
        'has_social_media': False,
        'has_droplet': False,
        'maintenance_onboarding': None,
        'social_onboarding': None,
    }
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return blank
    try:
        # Under a staff view-as session the target's account decides the
        # sidebar, not the operator's own. Reading `user.account` alone
        # would render the operator's service nav around the client's
        # pages — the sidebar would disagree with every page it links to.
        from .impersonation import active_target
        account = active_target(request) or getattr(user, 'account', None)
        if account is None:
            return blank

        # Per-service intake = the product's onboarding walkthrough
        # (onboarding app registry has full maintenance + social intakes).
        def _onboarding(product_type):
            from onboarding.models import Onboarding
            ob = (Onboarding.objects
                  .filter(user=user, product_type=product_type)
                  .order_by('-started_at')
                  .first())
            if ob is None:
                return None
            return {
                'product_type': ob.product_type,
                'tier_slug': ob.tier_slug,
                'complete': ob.completed_at is not None,
            }

        return {
            'has_website':      account.websites.exists(),
            'has_maintenance':  account.maintenance_plans.exists(),
            'has_social_media': account.social_media_plans.exists(),
            'has_droplet':      account.droplets.exists(),
            'maintenance_onboarding': _onboarding('maintenance'),
            'social_onboarding': _onboarding('social_media'),
        }
    except Exception:
        return blank


def impersonation(request):
    """Expose the live staff "view as client" session to every template.

    A context processor rather than a key in ``_portal_context`` on
    purpose: that helper is called explicitly by each portal view, and a
    view that builds its context some other way would silently render
    with no banner and no disabled buttons — the operator would be in a
    read-only session with nothing on screen saying so. A context
    processor cannot be forgotten.

    Returns:
        {
            'impersonating':         bool,
            'impersonated_account':  Account | None,
        }
    """
    try:
        from .impersonation import active_target
        target = active_target(request)
    except Exception:
        return {'impersonating': False, 'impersonated_account': None}
    return {
        'impersonating': target is not None,
        'impersonated_account': target,
    }
