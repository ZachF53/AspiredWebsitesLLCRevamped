"""
Shared admin chrome context.

`_admin_context` is merged in by every admin view, so it cannot live in
`views.py` now that views.py is being split: the extracted modules need it,
and importing it from views.py would make each of them depend on the module
that imports them. That is a circular import, and it presents as a
NameError at request time rather than at startup — which is exactly how the
first extraction pass broke (`manage.py check` passed; the tests did not).

Every badge count is individually wrapped. A fresh checkout may not have
run the migration that creates the table a count reads, and losing the
whole admin chrome over a missing badge number would be the wrong trade.
"""

import logging

from django.db.models import Count
from django.utils import timezone

from outreach.models import EmailReply, EmailSent

logger = logging.getLogger(__name__)


def _critical_health_count():
    """How many active non-tester clients currently in critical band.
    Used by the sidebar badge + the Intelligence dashboard banner."""
    from clients.models import ClientHealthScore
    # Latest score per client via Subquery would be ideal, but the
    # daily Celery beat means "any critical row from today" is a tight
    # enough proxy; we de-duplicate on client_id in Python.
    from django.utils import timezone as _tz
    today = _tz.now().date()
    rows = (ClientHealthScore.objects
            .filter(health_status='critical',
                    calculated_at__date=today,
                    client__status='active',
                    client__is_tester=False)
            .values_list('client_id', flat=True))
    return len(set(rows))


def _intel_pending_count():
    """Sidebar badge — admin needs to triage these."""
    try:
        from clients.models import IntelligenceSuggestion
        return IntelligenceSuggestion.objects.filter(
            status='pending_review').count()
    except Exception:
        return 0


def _high_priority_gaps_count():
    """Sidebar badge — un-actioned high-priority gaps."""
    try:
        from clients.models import CompetitorGapReport
        from django.db.models import Sum
        return (CompetitorGapReport.objects
                .filter(status='complete')
                .aggregate(s=Sum('high_priority_gaps'))['s'] or 0)
    except Exception:
        return 0


# ── Competitor CRUD (HTMX on the client detail page) ──────────────────────


def _active_proposals_count():
    """Sent + viewed proposals — used for the sidebar badge."""
    try:
        from clients.models import Proposal
        return Proposal.objects.filter(
            status__in=('sent', 'viewed')).count()
    except Exception:
        return 0


def _admin_context(active=None, **extra):
    """
    Base context every admin view should merge in. Provides:
      - active: which top-nav item to highlight
      - needs_you_count: badge number for the Needs You nav item
      - critical_health_count: badge number for the Intelligence nav
        item (Phase 7 — cheap today-only count, single query)
    """
    needs_you_count = EmailReply.objects.filter(
        needs_human=True, handled=False
    ).count()
    # Intake reviews (admin task generated when a client submits intake)
    # count toward the same Needs You badge.
    try:
        from clients.models import ClientProfile as _ClientProfile
        needs_you_count += _ClientProfile.objects.filter(
            needs_admin_review_at__isnull=False,
            admin_reviewed_at__isnull=True,
        ).count()
    except Exception:
        pass
    try:
        critical_health_count = _critical_health_count()
    except Exception:
        # ClientHealthScore migration may not have run yet on a fresh
        # checkout — never break the chrome over a missing table.
        critical_health_count = 0
    try:
        active_proposals_count = _active_proposals_count()
    except Exception:
        # Proposal table may not exist on a fresh checkout — never
        # break the chrome over a missing table.
        active_proposals_count = 0
    try:
        intel_pending_count = _intel_pending_count()
    except Exception:
        intel_pending_count = 0
    try:
        gap_high_count = _high_priority_gaps_count()
    except Exception:
        gap_high_count = 0
    try:
        # Approvals queue badge — every email the cold sender and reply
        # auto-drafter generate at the current trust level. Single
        # indexed query; defensive against the EmailSent.status migration
        # not having run yet on a fresh checkout.
        approvals_count = EmailSent.objects.filter(
            status='pending_approval').count()
    except Exception:
        approvals_count = 0
    ctx = {
        'active': active,
        'needs_you_count': needs_you_count,
        'critical_health_count': critical_health_count,
        'active_proposals_count': active_proposals_count,
        'intel_pending_count': intel_pending_count,
        'gap_high_count': gap_high_count,
        'approvals_count': approvals_count,
    }
    ctx.update(extra)
    return ctx


# ────────────────────────────────────────────────────────────────────────────
# Dashboard home
# ────────────────────────────────────────────────────────────────────────────
