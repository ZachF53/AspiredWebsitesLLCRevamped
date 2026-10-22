"""v2 Audit Tool usage — every run of the public /audit/ tool.

Read-only. Answers "is anyone using it?": counts for the last 7/30/60
days excluding our own testing, then the full list with whether the
visitor asked for the emailed report.
"""

from datetime import timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from admin_dashboard.decorators import admin_required


@admin_required
def audit_runs_list(request):
    from public.models import OWN_AUDIT_DOMAINS, AuditRun

    # Everything by default -- at this volume our own test runs are
    # most of the list, and hiding them made a real run look missing.
    hide_internal = request.GET.get('internal') == '0'
    internal = Q(by_staff=True) | Q(domain__in=OWN_AUDIT_DOMAINS)
    external = AuditRun.objects.exclude(internal)

    now = timezone.now()
    stats = []
    for days in (7, 30, 60):
        window = external.filter(created_at__gte=now - timedelta(days=days))
        stats.append({
            'days': days,
            'runs': window.count(),
            'emailed': window.filter(audit_lead__isnull=False).count(),
        })

    qs = external if hide_internal else AuditRun.objects.all()
    qs = qs.select_related('audit_lead')
    page = Paginator(qs, 50).get_page(request.GET.get('page', 1))

    return render(request, 'admin_dashboard/v2/audit_runs_list.html', {
        'page': page,
        'stats': stats,
        'hide_internal': hide_internal,
        'internal_count': AuditRun.objects.filter(internal).count(),
        'last_run': external.first(),
    })
