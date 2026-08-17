"""
Deployment dashboard views.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names so urls.py keeps working unchanged.
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .forms import DeploymentLogForm

# ────────────────────────────────────────────────────────────────────────────
# Deployment dashboard
# ────────────────────────────────────────────────────────────────────────────

GITHUB_REPO_DEFAULT = 'https://github.com/ZachF53/AspiredWebsitesLLCRevamped.git'


def _domain_from_url(url):
    """Extract a bare domain (no scheme, no www., no path) from a URL."""
    from urllib.parse import urlparse
    if not url:
        return ''
    netloc = urlparse(url).netloc or url
    netloc = netloc.split('/')[0]
    return netloc[4:] if netloc.startswith('www.') else netloc


@admin_required
def deploy_home(request):
    """Deploy landing page — 3 deploy-type cards + recent deployments."""
    from .models import DeploymentLog
    from clients.models import ClientProfile
    return render(request, 'admin_dashboard/deploy_home.html', _admin_context(
        'deploy',
        recent=DeploymentLog.objects.select_related('client')[:10],
        clients=ClientProfile.objects.order_by('firm_name'),
    ))


@admin_required
def deploy_fresh(request):
    """Fresh-server deploy runbook with live-fill command blocks."""
    from django.utils.text import slugify
    from clients.models import ClientProfile
    options = []
    for client in ClientProfile.objects.filter(do_droplet_ip__isnull=False):
        project = client    # ← alias post-2026-05-25 refactor (project fields on ClientProfile)
        options.append({
            'id': client.id,
            'name': client.firm_name,
            'slug': slugify(client.firm_name),
            'ip': client.do_droplet_ip or '',
            'domain': _domain_from_url(project.live_url) if project else '',
        })
    return render(request, 'admin_dashboard/deploy_fresh.html', _admin_context(
        'deploy',
        client_options=options,
        github_default=GITHUB_REPO_DEFAULT,
    ))


@admin_required
def deploy_redeploy(request):
    """Re-deploy runbook — push + run deploy.sh."""
    return render(request, 'admin_dashboard/deploy_redeploy.html',
                  _admin_context('deploy'))


@admin_required
def deploy_client(request, client_id):
    """Client-site deploy runbook, pre-filled from the ClientProfile."""
    from django.utils.text import slugify
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    project = client    # ← alias post-2026-05-25 refactor (project fields on ClientProfile)
    return render(request, 'admin_dashboard/deploy_client.html', _admin_context(
        'deploy',
        deploy_client=client,
        prefill_ip=client.do_droplet_ip or '',
        prefill_domain=_domain_from_url(project.live_url) if project else '',
        prefill_client=slugify(client.firm_name),
        github_default=GITHUB_REPO_DEFAULT,
    ))


@admin_required
def deploy_history(request):
    """Table of all DeploymentLog records + a manual log form."""
    from .models import DeploymentLog
    return render(request, 'admin_dashboard/deploy_history.html', _admin_context(
        'deploy',
        logs=DeploymentLog.objects.select_related('client'),
        form=DeploymentLogForm(),
        logged=request.GET.get('logged'),
    ))


@admin_required
@require_POST
def deploy_log_create(request):
    """Create a DeploymentLog from the manual log form."""
    from .models import DeploymentLog
    form = DeploymentLogForm(request.POST)
    if form.is_valid():
        form.save()
        return redirect(
            f"{reverse('admin_dashboard:deploy_history')}?logged=1"
        )
    return render(request, 'admin_dashboard/deploy_history.html', _admin_context(
        'deploy',
        logs=DeploymentLog.objects.select_related('client'),
        form=form,
    ))


