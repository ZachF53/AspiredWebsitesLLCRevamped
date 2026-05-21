"""
Admin dashboard auth decorators.

`admin_required` is just `staff_member_required` with our custom login URL
baked in so unauthenticated users go to our branded /login/ page instead of
Django's default admin login.
"""

from django.contrib.admin.views.decorators import staff_member_required


def admin_required(view_func):
    """
    Require an authenticated staff user. Non-staff get a 403; unauthenticated
    users are redirected to the unified /login/ page (which then routes them
    back to the admin dashboard after auth via the ?next= param).
    """
    return staff_member_required(view_func, login_url='/login/')
