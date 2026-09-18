"""v2 Billing — everything outstanding across all clients. Read-only."""

from django.shortcuts import render
from django.utils import timezone

from admin_dashboard.decorators import admin_required


@admin_required
def billing_list(request):
    from billing.models import MiniInvoice
    from clients.models import OnboardingInvoice

    now = timezone.now()

    onboarding_invoices = (
        OnboardingInvoice.objects
        .exclude(status='paid')
        .exclude(status='canceled')
        .select_related('account_new', 'website_new')
        .order_by('sent_at'))

    mini_invoices = (
        MiniInvoice.objects
        .exclude(status__in=['paid', 'cancelled'])
        .select_related('account_new', 'website_new')
        .order_by('created_at'))

    rows = []
    for inv in onboarding_invoices:
        anchor = inv.sent_at or inv.created_at
        rows.append({
            'kind': 'Onboarding invoice',
            'subject': (inv.website_new.name if inv.website_new
                        else (inv.account_new.name if inv.account_new else '—')),
            'amount': inv.total_amount,
            'status': inv.status,
            'age_days': (now - anchor).days if anchor else 0,
            'anchor': anchor,
        })
    for inv in mini_invoices:
        rows.append({
            'kind': 'Out-of-scope invoice',
            'subject': (inv.website_new.name if inv.website_new
                        else (inv.account_new.name if inv.account_new else '—')),
            'amount': inv.amount,
            'status': inv.status,
            'age_days': (now - inv.created_at).days if inv.created_at else 0,
            'anchor': inv.created_at,
        })
    rows.sort(key=lambda r: r['age_days'], reverse=True)

    return render(request, 'admin_dashboard/v2/billing_list.html', {
        'rows': rows,
    })
