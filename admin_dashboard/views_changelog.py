"""
Per-client site changelog admin.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

import re
import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .decorators import admin_required
from .utils import _is_uuid

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Site changelog — per-client website change log
# ────────────────────────────────────────────────────────────────────────────

# Matches a deploy.sh step line, e.g. "[3/7] Running migrations..."
_DEPLOY_STEP_RE = re.compile(r'^\s*\[(\d+)/(\d+)\]\s*(.+?)\s*$')


def _parse_deploy_log(text):
    """Pull the '[n/n] description' step lines out of raw deploy.sh output."""
    steps = []
    for line in (text or '').splitlines():
        match = _DEPLOY_STEP_RE.match(line)
        if match:
            desc = match.group(3).strip()
            if desc:
                steps.append(desc)
    return steps


@admin_required
def changelog_list(request):
    """All changelog entries across every client, with filters."""
    from clients.models import ClientProfile, SiteChangelogEntry
    from django.utils.dateparse import parse_date

    entries = SiteChangelogEntry.objects.select_related('client')

    client_filter = request.GET.get('client', '')
    type_filter = request.GET.get('change_type', '')
    visible_filter = request.GET.get('visible', '')
    date_from = request.GET.get('from', '')
    date_to = request.GET.get('to', '')

    if client_filter and _is_uuid(client_filter):
        entries = entries.filter(client_id=client_filter)
    if type_filter:
        entries = entries.filter(change_type=type_filter)
    if visible_filter == 'yes':
        entries = entries.filter(is_client_visible=True)
    elif visible_filter == 'no':
        entries = entries.filter(is_client_visible=False)
    if parse_date(date_from):
        entries = entries.filter(date_of_change__gte=date_from)
    if parse_date(date_to):
        entries = entries.filter(date_of_change__lte=date_to)

    return render(request, 'admin_dashboard/changelog_list.html', _admin_context(
        'changelog',
        entries=entries,
        clients=ClientProfile.objects.order_by('firm_name'),
        change_type_choices=SiteChangelogEntry.CHANGE_TYPE_CHOICES,
        client_filter=client_filter,
        type_filter=type_filter,
        visible_filter=visible_filter,
        date_from=date_from,
        date_to=date_to,
    ))


@admin_required
def website_changelog(request, website_id):
    """Changelog entries for a single website."""
    from clients.account_models import Website
    from clients.models import SiteChangelogEntry
    website = get_object_or_404(Website, id=website_id)
    return render(request, 'admin_dashboard/changelog_list.html', _admin_context(
        'changelog',
        entries=SiteChangelogEntry.objects.filter(website_new=website),
        single_website=website,
        change_type_choices=SiteChangelogEntry.CHANGE_TYPE_CHOICES,
    ))


@admin_required
def changelog_add_website(request, website_id):
    """Add a changelog entry pre-scoped to a website."""
    from clients.account_models import Website
    from clients.models import ClientProfile
    from .forms import SiteChangelogForm

    website = get_object_or_404(Website, id=website_id)
    cp = website.account.legacy_client_profile

    if request.method == 'POST':
        form = SiteChangelogForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.website_new = website
            if entry.client_id is None:
                entry.client = cp
            entry.save()
            return redirect(
                'admin_dashboard:website_changelog', website_id=website.id)
    else:
        form = SiteChangelogForm(
            initial={'client': cp} if cp else None)

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=form,
        mode='add',
        preset_client=cp,
        form_action=reverse(
            'admin_dashboard:changelog_add_website', args=[website.id]),
        clients=ClientProfile.objects.order_by('firm_name'),
    ))


@admin_required
def changelog_add(request):
    """Add a changelog entry (global — pick the client in the form)."""
    from clients.models import ClientProfile
    from .forms import SiteChangelogForm

    if request.method == 'POST':
        form = SiteChangelogForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard:changelog_list')
    else:
        form = SiteChangelogForm()

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=form,
        mode='add',
        preset_client=None,
        form_action=reverse('admin_dashboard:changelog_add'),
        clients=ClientProfile.objects.order_by('firm_name'),
    ))


@admin_required
def changelog_edit(request, entry_id):
    """Edit an existing changelog entry."""
    from clients.models import SiteChangelogEntry
    from .forms import SiteChangelogForm

    entry = get_object_or_404(SiteChangelogEntry, id=entry_id)

    if request.method == 'POST':
        form = SiteChangelogForm(request.POST, instance=entry)
        if form.is_valid():
            form.save()
            if request.POST.get('next') == 'website' and entry.website_new_id:
                return redirect('admin_dashboard:website_changelog',
                                website_id=entry.website_new_id)
            return redirect('admin_dashboard:changelog_list')
    else:
        form = SiteChangelogForm(instance=entry)

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=form,
        mode='edit',
        entry=entry,
        form_action=reverse('admin_dashboard:changelog_edit', args=[entry.id]),
    ))


@admin_required
@require_POST
def changelog_delete(request, entry_id):
    """Delete a changelog entry (POST + CSRF only)."""
    from clients.models import SiteChangelogEntry
    entry = get_object_or_404(SiteChangelogEntry, id=entry_id)
    website_id = entry.website_new_id
    came_from_website = request.POST.get('next') == 'website'
    entry.delete()
    if came_from_website and website_id:
        return redirect(
            'admin_dashboard:website_changelog', website_id=website_id)
    return redirect('admin_dashboard:changelog_list')


@admin_required
@require_POST
def changelog_import(request):
    """
    Parse pasted deploy.sh output into deployment changelog entries.

    Two-step: `step=preview` parses + shows a preview; `step=save` re-parses
    the same text and creates one entry per [n/n] step.
    """
    from clients.models import ClientProfile, SiteChangelogEntry
    from .forms import SiteChangelogForm

    raw_log = request.POST.get('raw_log', '')
    client_id = request.POST.get('import_client', '')
    step = request.POST.get('step', 'preview')

    client = None
    if client_id and _is_uuid(client_id):
        client = ClientProfile.objects.filter(id=client_id).first()

    parsed = _parse_deploy_log(raw_log)

    if step == 'save' and client and parsed:
        today = timezone.localdate()
        try:
            _acct = client.migrated_account
        except Exception:
            _acct = None
        site = (_acct.websites.order_by('created_at').first()
                if _acct else None)
        for title in parsed:
            # Imported deploy steps land as an internal audit trail — staff
            # flip individual entries visible to surface them to the client.
            SiteChangelogEntry.objects.create(
                client=client,
                website_new=site,
                change_type='deployment',
                title=title,
                is_client_visible=False,
                date_of_change=today,
            )
        if site is not None:
            return redirect(
                'admin_dashboard:website_changelog', website_id=site.id)
        return redirect('admin_dashboard:changelog_list')

    import_error = None
    if not parsed:
        import_error = 'No "[n/n]" deploy steps were found in that text.'
    elif not client:
        import_error = 'Choose a client to import these steps into.'

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=SiteChangelogForm(initial={'client': client} if client else None),
        mode='add',
        preset_client=client,
        form_action=reverse('admin_dashboard:changelog_add'),
        clients=ClientProfile.objects.order_by('firm_name'),
        import_preview=parsed,
        import_raw=raw_log,
        import_client=client,
        import_error=import_error,
    ))


