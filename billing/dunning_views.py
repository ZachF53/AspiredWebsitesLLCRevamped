"""
Operator surface for payment-failure dunning.

Day 14 (maintenance page) and Day 21 (power off) are reversible and run
themselves. Day 30 (destroy droplet) and Day 60 (delete snapshot) are
not, so the sweep stops at them and waits here.

That hold exists because of what happened on 2026-09-02: a dunning chain
armed itself against a client who had paid, and only a guard check
elsewhere kept it from running. The sweep now verifies against Stripe
before it acts, but "verify twice, and put a person in front of the
irreversible step" is cheap next to destroying a paying client's site.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required
from billing.dunning_models import DunningEvent


@admin_required
def dunning_list(request):
    """Open windows, steps waiting on a human, and recent history."""
    from clients.account_models import Account

    pending = (DunningEvent.objects
               .filter(status=DunningEvent.STATUS_AWAITING_APPROVAL)
               .select_related('account', 'website')
               .order_by('created_at'))

    open_windows = []
    now = timezone.now()
    for account in Account.objects.filter(
            payment_failure_started_at__isnull=False):
        open_windows.append({
            'account': account,
            'started_at': account.payment_failure_started_at,
            'days': (now - account.payment_failure_started_at).days,
            'events': (DunningEvent.objects
                       .filter(account=account,
                               window_started_at=account.payment_failure_started_at)
                       .select_related('website')
                       .order_by('created_at')),
        })

    recent = (DunningEvent.objects
              .exclude(status=DunningEvent.STATUS_AWAITING_APPROVAL)
              .select_related('account', 'website')
              .order_by('-created_at')[:50])

    return render(request, 'admin_dashboard/dunning.html', {
        'pending': pending,
        'open_windows': open_windows,
        'recent': recent,
    })


@admin_required
@require_POST
def dunning_approve(request, event_id):
    """Run a held destructive step.

    `approve_stage` re-checks Stripe before acting — the approval may be
    hours old, and the whole point of this module is never to act on a
    decision made earlier than the action itself.
    """
    from billing.dunning import approve_stage

    event = get_object_or_404(
        DunningEvent, pk=event_id,
        status=DunningEvent.STATUS_AWAITING_APPROVAL)
    ok, msg = approve_stage(event, request.user)
    (messages.success if ok else messages.error)(request, msg)
    return redirect('admin_dashboard:dunning')


@admin_required
@require_POST
def dunning_cancel(request, event_id):
    """Decline a held destructive step."""
    from billing.dunning import cancel_stage

    event = get_object_or_404(
        DunningEvent, pk=event_id,
        status=DunningEvent.STATUS_AWAITING_APPROVAL)
    reason = (request.POST.get('reason') or '').strip()
    ok, msg = cancel_stage(event, request.user, reason)
    (messages.success if ok else messages.error)(request, msg)
    return redirect('admin_dashboard:dunning')


@admin_required
@require_POST
def dunning_close_window(request, account_id):
    """Manually clear an account's failure window.

    The escape hatch for when Stripe and reality disagree — a client who
    paid by bank transfer, say. Restores any site the window suspended
    and charges no reinstatement fee.
    """
    from billing.dunning import close_window
    from clients.account_models import Account

    account = get_object_or_404(
        Account, pk=account_id, payment_failure_started_at__isnull=False)
    close_window(account, f'closed manually by {request.user}')
    messages.success(
        request, f'Dunning window closed for {account}. Any suspended site '
                 f'has been restored.')
    return redirect('admin_dashboard:dunning')
