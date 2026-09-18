"""
Querysets and computations for the v2 dashboard. Views fetch and render
only — every non-trivial query lives here so both dashboards (and any
future caller) can share it without duplicating criteria.

Read-only throughout. Nothing here writes to the database or calls
Stripe.
"""

from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone


# ─────────────────────────────────────────────────────────────────────────
# Action list
# ─────────────────────────────────────────────────────────────────────────

def _action(kind, label, subject, waiting_since, url, detail=''):
    now = timezone.now()
    age_days = (now - waiting_since).days if waiting_since else 0
    return {
        'kind': kind,
        'label': label,
        'subject': subject,
        'detail': detail,
        'waiting_since': waiting_since,
        'age_days': age_days,
        'url': url,
    }


def _dunning_approvals():
    from billing.dunning_models import DunningEvent

    items = []
    qs = (DunningEvent.objects
          .filter(status=DunningEvent.STATUS_AWAITING_APPROVAL)
          .select_related('account', 'website')
          .order_by('created_at'))
    for event in qs:
        subject = event.website.name if event.website else event.account.name
        items.append(_action(
            'dunning', 'Dunning approval needed', subject,
            event.created_at, reverse('admin_dashboard:dunning'),
            detail=event.get_stage_display()
            if hasattr(event, 'get_stage_display') else event.stage,
        ))
    return items


def _unresolved_alerts():
    from core.models import SystemAlert

    items = []
    qs = (SystemAlert.objects
          .filter(resolved_at__isnull=True)
          .order_by('created_at'))
    for alert in qs:
        items.append(_action(
            'alert', f'System alert ({alert.severity})', alert.source,
            alert.created_at, reverse('admin_dashboard:system_alerts'),
            detail=alert.message,
        ))
    return items


def _critical_scans():
    from django.db.models import Q

    from reporting.models import VulnerabilityScan

    items = []
    qs = (VulnerabilityScan.objects
          .filter(status='complete', been_reviewed=False)
          .filter(Q(critical_count__gt=0) | Q(high_count__gt=0))
          .select_related('website_new', 'website_new__account')
          .order_by('completed_at'))
    for scan in qs:
        site = scan.website_new
        subject = site.name if site else (scan.target_url or scan.target_ip)
        items.append(_action(
            'scan', 'Unreviewed scan with critical/high findings', subject,
            scan.completed_at, reverse('admin_dashboard:scan_detail',
                                        args=[scan.id]),
            detail=f'{scan.critical_count} critical, {scan.high_count} high',
        ))
    return items


def _site_status_mismatch():
    from clients.account_models import Website

    items = []
    qs = (Website.objects
          .filter(status='active')
          .exclude(site_status='live')
          .select_related('account')
          .order_by('updated_at'))
    for site in qs:
        items.append(_action(
            'site_status', 'Active site is not showing live', site.name,
            site.updated_at,
            reverse('admin_dashboard:v2_website_detail', args=[site.id]),
            detail=f'site_status={site.site_status}',
        ))
    return items


def _outstanding_invoices(cutoff_days=30):
    """OnboardingInvoice rows sent but never paid, 30+ days old.

    No due_date field exists on OnboardingInvoice — `sent_at` is the best
    available anchor, so "30+ days outstanding" here means "sent 30+ days
    ago and still unpaid," not "30 days past a due date."
    """
    from clients.models import OnboardingInvoice

    cutoff = timezone.now() - timedelta(days=cutoff_days)
    items = []
    qs = (OnboardingInvoice.objects
          .filter(status='sent', sent_at__lte=cutoff)
          .select_related('account_new', 'website_new')
          .order_by('sent_at'))
    for inv in qs:
        subject = (inv.website_new.name if inv.website_new
                   else (inv.account_new.name if inv.account_new else '—'))
        items.append(_action(
            'invoice', 'Invoice outstanding 30+ days', subject,
            inv.sent_at,
            reverse('admin_dashboard:v2_billing_list'),
            detail=f'${inv.total_amount}',
        ))
    return items


def _accounts_pending_setup():
    from clients.account_models import Account

    items = []
    qs = (Account.objects
          .filter(onboarding_status='pending_setup')
          .order_by('created_at'))
    for account in qs:
        items.append(_action(
            'account_setup', 'Account pending setup', account.name,
            account.created_at,
            reverse('admin_dashboard:v2_account_detail', args=[account.id]),
        ))
    return items


def _websites_pending_intake():
    from clients.account_models import Website

    items = []
    qs = (Website.objects
          .filter(onboarding_status='pending_intake')
          .select_related('account')
          .order_by('created_at'))
    for site in qs:
        items.append(_action(
            'intake', 'Website pending intake', site.name,
            site.created_at,
            reverse('admin_dashboard:v2_website_detail', args=[site.id]),
        ))
    return items


def _needs_admin_review():
    """Same criteria as v1's Needs You queue — see
    admin_dashboard/views.py `_pending_intake_reviews()`. Reused rather
    than reinvented."""
    from clients.account_models import Website

    items = []
    qs = (Website.objects
          .filter(needs_admin_review_at__isnull=False,
                  admin_reviewed_at__isnull=True)
          .select_related('account')
          .order_by('needs_admin_review_at'))
    for site in qs:
        items.append(_action(
            'needs_review', 'Needs admin review', site.name,
            site.needs_admin_review_at,
            reverse('admin_dashboard:v2_website_detail', args=[site.id]),
        ))
    return items


def get_action_list():
    """The merged, urgency-sorted action list for the v2 landing page.

    Urgency = longest-waiting first. Every source is best-effort — one
    broken query must not blank the whole list, so each is wrapped and
    logged rather than let to propagate.
    """
    import logging
    logger = logging.getLogger(__name__)

    sources = (
        _dunning_approvals,
        _unresolved_alerts,
        _critical_scans,
        _site_status_mismatch,
        _outstanding_invoices,
        _accounts_pending_setup,
        _websites_pending_intake,
        _needs_admin_review,
    )
    items = []
    for source in sources:
        try:
            items.extend(source())
        except Exception:
            logger.exception('v2 action list source failed: %s',
                              source.__name__)
    items.sort(key=lambda i: i['age_days'], reverse=True)
    return items


# ─────────────────────────────────────────────────────────────────────────
# Counts
# ─────────────────────────────────────────────────────────────────────────

def get_counts():
    from clients.account_models import Account, Website

    return {
        'total_clients': Account.objects.count(),
        'active_websites': Website.objects.filter(status='active').count(),
        'paying_clients': (Account.objects
                            .filter(websites__maintenance_active=True)
                            .distinct().count()),
        'pending_setup': Account.objects.filter(
            onboarding_status='pending_setup').count(),
        'pending_intake': Website.objects.filter(
            onboarding_status='pending_intake').count(),
    }


# ─────────────────────────────────────────────────────────────────────────
# Money — local ledger only, never Stripe. See v2 build report for why.
# ─────────────────────────────────────────────────────────────────────────

MONEY_CACHE_KEY = 'admin_dashboard_v2_money'
MONEY_CACHE_SECONDS = 300


def get_money_summary():
    """MRR + cash collected this month, read entirely from local data
    (clients.revenue.get_current_mrr + the PaymentRecord ledger) —
    deliberately NOT a live Stripe API read. Cached 5 minutes.
    """
    cached = cache.get(MONEY_CACHE_KEY)
    if cached is not None:
        return cached

    from django.db.models import Sum

    from clients.models import PaymentRecord
    from clients.revenue import get_current_mrr

    now = timezone.now()
    mrr = get_current_mrr()
    collected = (PaymentRecord.objects
                 .filter(status='paid', paid_at__year=now.year,
                         paid_at__month=now.month)
                 .aggregate(total=Sum('amount'))['total']) or 0

    summary = {
        'mrr_total': mrr['mrr_total'],
        'active_maintenance_clients': mrr['active_maintenance_clients'],
        'cash_collected_this_month': collected,
        'computed_at': now,
    }
    cache.set(MONEY_CACHE_KEY, summary, MONEY_CACHE_SECONDS)
    return summary
