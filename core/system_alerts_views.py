"""Admin views to list + resolve SystemAlerts."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required
from core.models import SystemAlert


@admin_required
def alerts_list(request):
    """All recent alerts, unresolved first."""
    unresolved = SystemAlert.objects.filter(
        resolved_at__isnull=True).order_by('-created_at')[:100]
    resolved = SystemAlert.objects.filter(
        resolved_at__isnull=False).order_by('-resolved_at')[:25]
    return render(request, 'admin_dashboard/system_alerts.html', {
        'unresolved': unresolved,
        'resolved': resolved,
        'unresolved_count': unresolved.count(),
    })


@admin_required
@require_POST
def alert_resolve(request, alert_id):
    alert = get_object_or_404(SystemAlert, pk=alert_id, resolved_at__isnull=True)
    alert.resolved_at = timezone.now()
    alert.resolved_by = request.user
    alert.save(update_fields=['resolved_at', 'resolved_by'])
    messages.success(request, 'Alert resolved.')
    return redirect('admin_dashboard:system_alerts')


@admin_required
@require_POST
def alert_resolve_all(request):
    n = SystemAlert.objects.filter(resolved_at__isnull=True).update(
        resolved_at=timezone.now(),
        resolved_by=request.user,
    )
    messages.success(request, f'Resolved {n} alert{"s" if n != 1 else ""}.')
    return redirect('admin_dashboard:system_alerts')
