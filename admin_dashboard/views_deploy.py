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
    from clients.account_models import Website
    return render(request, 'admin_dashboard/deploy_home.html', _admin_context(
        'deploy',
        recent=DeploymentLog.objects.select_related(
            'website_new', 'website_new__account')[:10],
        clients=(Website.objects.select_related('account')
                 .order_by('account__name', 'name')),
    ))


@admin_required
def deploy_fresh(request):
    """Fresh-server deploy runbook with live-fill command blocks."""
    from django.utils.text import slugify
    from clients.account_models import Website
    # A deploy targets one droplet, and a droplet belongs to one site. The
    # account-level list offered a single entry per client, so the second
    # site of a two-site account could not be deployed from here at all.
    options = []
    for client in (Website.objects
                   .filter(do_droplet_ip__isnull=False)
                   .select_related('account')):
        options.append({
            'id': client.id,
            'name': client.name,
            'slug': slugify(client.name),
            'ip': client.do_droplet_ip or '',
            'domain': _domain_from_url(client.url),
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
    """Client-site deploy runbook, pre-filled from the Website."""
    from django.utils.text import slugify
    from clients.account_models import Website
    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    return render(request, 'admin_dashboard/deploy_client.html', _admin_context(
        'deploy',
        deploy_client=client,
        prefill_ip=client.do_droplet_ip or '',
        prefill_domain=_domain_from_url(client.url),
        prefill_client=slugify(client.name),
        github_default=GITHUB_REPO_DEFAULT,
    ))


@admin_required
def deploy_history(request):
    """Table of all DeploymentLog records + a manual log form."""
    from .models import DeploymentLog
    return render(request, 'admin_dashboard/deploy_history.html', _admin_context(
        'deploy',
        logs=DeploymentLog.objects.select_related(
            'website_new', 'website_new__account'),
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
        logs=DeploymentLog.objects.select_related(
            'website_new', 'website_new__account'),
        form=form,
    ))


