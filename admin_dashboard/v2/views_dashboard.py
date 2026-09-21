"""v2 landing dashboard — action list, counts, and (async) money block."""

from django.shortcuts import render

from admin_dashboard.decorators import admin_required

from . import services


@admin_required
def home(request):
    ctx = {
        'action_list': services.get_action_list(),
        'counts': services.get_counts(),
    }
    return render(request, 'admin_dashboard/v2/dashboard.html', ctx)


@admin_required
def money_partial(request):
    """HTMX-loaded after the page renders, so a slow/down data source
    never blocks the dashboard itself. Read-only — the Stripe-sourced
    figure is precomputed by a Celery beat sweep, not fetched live
    here; see services.get_money_summary."""
    try:
        money = services.get_money_summary()
        error = None
    except Exception:
        import logging
        logging.getLogger(__name__).exception('v2 money partial failed')
        money = None
        error = 'Could not load revenue figures right now.'
    return render(request, 'admin_dashboard/v2/_money.html', {
        'money': money, 'error': error,
    })
