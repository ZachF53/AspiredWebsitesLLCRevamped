"""Access-control decorators for the client portal."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login

from .models import ClientProfile


def client_required(view_func):
    """
    Gate a portal view: the user must be authenticated AND have a
    ClientProfile. Anyone else is bounced to /login/ with a ?next= back here.

    On success the resolved ClientProfile is attached as
    `request.client_profile` so portal views don't each re-query it.
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
        return view_func(request, *args, **kwargs)

    return _wrapped
