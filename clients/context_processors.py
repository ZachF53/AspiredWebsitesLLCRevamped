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
    """Inspect the user's Account; return per-service booleans."""
    blank = {
        'has_website': False,
        'has_maintenance': False,
        'has_social_media': False,
        'has_droplet': False,
    }
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return blank
    try:
        account = getattr(user, 'account', None)
        if account is None:
            return blank
        return {
            'has_website':      account.websites.exists(),
            'has_maintenance':  account.maintenance_plans.exists(),
            'has_social_media': account.social_media_plans.exists(),
            'has_droplet':      account.droplets.exists(),
        }
    except Exception:
        return blank
