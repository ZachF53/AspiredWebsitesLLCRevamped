"""
Data Health — one page for the checks that otherwise need an SSH session.

The cutover produced several signals that only existed as management
commands on the server: the parity gate, payments with no ledger evidence,
accounts a scheduled task skipped, sync jobs that failed and stopped
retrying. Each one is the kind of thing that stays broken precisely
because nobody thinks to go and look.

This page is read-only and derives everything from the same functions the
commands use, so the dashboard cannot drift from the gate. It runs the
parity audit live rather than caching it — the dataset is small (tens of
accounts) and a stale health page is worse than a slow one.
"""

from django.shortcuts import render

from admin_dashboard.decorators import admin_required


def _parity_section():
    from clients.parity import audit_account_website_parity

    report = audit_account_website_parity(detail_limit=5)
    return {
        'errors': report.error_count,
        'warnings': report.warning_count,
        'operational': report.operational_count,
        'findings': report.findings,
        'counts': report.counts,
        'clean': not report.findings,
    }


def _payment_evidence_section():
    """Websites claiming settled payment the ledger cannot support."""
    from clients.account_models import Website
    from clients.payment_evidence import (
        is_fully_paid_without_evidence, ledger_evidence_for)

    unverified, verified = [], 0
    for site in Website.objects.filter(
            payment_status='fully_paid').select_related('account'):
        if is_fully_paid_without_evidence(site):
            unverified.append(site)
        else:
            verified += 1
    return {
        'unverified': unverified,
        'unverified_count': len(unverified),
        'verified_count': verified,
        'evidence_for': ledger_evidence_for,
    }


def _sync_section():
    """Moonieful sync jobs that failed, and ones stuck retrying."""
    from sync.models import SyncJob

    failed = list(
        SyncJob.objects.filter(status='failed')
        .order_by('-last_attempt_at')[:10])
    pending = SyncJob.objects.filter(status='pending').count()
    stuck = SyncJob.objects.filter(status='pending', attempts__gte=3).count()
    return {
        'failed': failed,
        'failed_count': SyncJob.objects.filter(status='failed').count(),
        'pending': pending,
        'stuck': stuck,
    }


def _legacy_section():
    """How far the Account/Website cutover has left to run.

    This function reads ClientProfile and Project on purpose, and it is
    NOT exempted from `check_legacy_removal_readiness`. Being counted as a
    blocker is correct: it genuinely breaks when the tables are dropped.
    Unlike every other blocker, though, the fix is deletion rather than
    conversion — once the legacy tables are gone this whole section is
    reporting on something that no longer exists, so it comes out in the
    same change that drops them.
    """
    from clients.canonical_stamping import build_plan
    from clients.models import ClientProfile, Project

    orphans = 0
    for model, (account_field, website_field, _) in build_plan().items():
        filters = {'client__isnull': False}
        if account_field:
            filters[f'{account_field}__isnull'] = True
        elif website_field:
            filters[f'{website_field}__isnull'] = True
        else:
            continue
        orphans += model.objects.filter(**filters).count()

    from clients.account_models import Account

    return {
        'legacy_profiles': ClientProfile.objects.count(),
        'legacy_projects': Project.objects.count(),
        'accounts': Account.objects.count(),
        'accounts_without_profile': Account.objects.filter(
            legacy_client_profile__isnull=True).count(),
        'orphan_rows': orphans,
    }


def _outreach_sequence_section():
    """Leads whose sequence clock has stopped despite mail going out.

    ``Lead.sequence_step`` advances only on a CONFIRMED SEND (see
    outreach/dispatcher.py). That makes it a silent single point of
    failure: if the code that advances it is ever missing — most
    plausibly when dispatcher.py is retired for Instantly and the advance
    has to be re-anchored to the "email sent" webhook (see
    COLD_OUTREACH_AGENT.md §4 step 6) — nothing errors. Mail keeps going
    out, every lead sits at the same step forever, and no follow-up ever
    fires. There is no exception to catch and no log line to notice.

    So we assert the invariant directly: for any lead with a SENT email at
    step N, ``lead.sequence_step`` must be >= N. A non-zero count here
    means the advance is broken.
    """
    from django.db.models import F

    from outreach.models import EmailSent

    frozen = (
        EmailSent.objects
        .filter(status='sent', kind='cold')
        .filter(sequence_step__gt=F('lead__sequence_step'))
        .select_related('lead')
        .order_by('-sent_at')
    )
    sample = list(frozen[:5])
    return {
        'frozen_count': frozen.count(),
        'frozen_sample': sample,
        'clean': not sample,
    }


def _alerts_section():
    from core.models import SystemAlert

    return {
        'unresolved': SystemAlert.objects.filter(
            resolved_at__isnull=True).count(),
        'recent': list(
            SystemAlert.objects.filter(resolved_at__isnull=True)
            .order_by('-created_at')[:5]),
    }


@admin_required
def data_health(request):
    parity = _parity_section()
    payments = _payment_evidence_section()
    sync = _sync_section()
    legacy = _legacy_section()
    alerts = _alerts_section()
    outreach_seq = _outreach_sequence_section()

    # One headline so the page answers "is anything wrong?" before it
    # answers "what exactly?".
    problems = (
        parity['errors'] + parity['warnings'] + payments['unverified_count']
        + sync['failed_count'] + sync['stuck'] + alerts['unresolved']
        + legacy['orphan_rows'] + outreach_seq['frozen_count']
    )

    return render(request, 'admin_dashboard/data_health.html', {
        'parity': parity,
        'payments': payments,
        'sync': sync,
        'legacy': legacy,
        'alerts': alerts,
        'outreach_seq': outreach_seq,
        'problems': problems,
    })
