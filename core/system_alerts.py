"""
SystemAlert — operator-facing surface for system errors so they
don't require an SSH session to discover.

Anywhere we silently caught + logged an exception (email send
failures, webhook crashes, Calendar push failures) now also calls
``record_alert()`` so the alert appears as a banner on
``/admin-dashboard/`` until the operator dismisses it.

The model lives in core/ because it's cross-cutting infrastructure
that any app may use.
"""

import logging

logger = logging.getLogger(__name__)


def record_alert(severity, source, message, detail=''):
    """
    Write a SystemAlert row. Best-effort — never raises.

    Args:
        severity: 'info' | 'warning' | 'error' | 'critical'
        source:   short string identifying where it came from
                  (e.g. 'scheduler.google_calendar')
        message:  one-line headline
        detail:   full traceback or response body (truncated to 4000c)
    """
    try:
        from core.models import SystemAlert
        SystemAlert.objects.create(
            severity=severity,
            source=source[:80],
            message=message[:255],
            detail=(detail or '')[:4000],
        )
    except Exception:  # noqa: BLE001
        # If we can't even write the alert, fall back to the log
        logger.exception(
            'record_alert failed (severity=%s source=%s msg=%s)',
            severity, source, message)


def recent_unresolved_count():
    """Number of unresolved alerts in the last 7 days — used by the
    dashboard banner. Defensive in case the table doesn't exist yet."""
    try:
        import datetime as _dt
        from django.utils import timezone
        from core.models import SystemAlert
        cutoff = timezone.now() - _dt.timedelta(days=7)
        return SystemAlert.objects.filter(
            resolved_at__isnull=True, created_at__gte=cutoff,
        ).count()
    except Exception:
        return 0
