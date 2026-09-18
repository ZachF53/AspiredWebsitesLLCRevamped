"""
The v2/v1 dashboard toggle. A session flag, not a URL prefix — see the
module docstring on `admin_dashboard.navigation.navigation` for why.
"""

from django.shortcuts import redirect

from admin_dashboard.decorators import admin_required


@admin_required
def use_v2(request):
    request.session['dashboard_version'] = 'v2'
    return redirect('admin_dashboard:v2_home')


@admin_required
def use_v1(request):
    request.session['dashboard_version'] = 'v1'
    return redirect('admin_dashboard:home')
