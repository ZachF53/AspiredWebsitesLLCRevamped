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


def _selectable_websites():
    """Every website, ordered the way a picker reads."""
    from clients.account_models import Website

    return (Website.objects
            .select_related('account')
            .order_by('account__name', 'name'))


@admin_required
def changelog_list(request):
    """All changelog entries across every website, with filters."""
    from clients.models import SiteChangelogEntry
    from django.utils.dateparse import parse_date

    entries = SiteChangelogEntry.objects.select_related(
        'website_new', 'website_new__account')

    client_filter = request.GET.get('website', '')
    type_filter = request.GET.get('change_type', '')
    visible_filter = request.GET.get('visible', '')
    date_from = request.GET.get('from', '')
    date_to = request.GET.get('to', '')

    if client_filter and _is_uuid(client_filter):
        entries = entries.filter(website_new_id=client_filter)
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
        clients=_selectable_websites(),
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
    from .forms import SiteChangelogForm

    website = get_object_or_404(
        Website.objects.select_related('account'), id=website_id)

    if request.method == 'POST':
        form = SiteChangelogForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            # The URL already says which site this is for, so it wins over
            # whatever the form's picker happens to hold.
            entry.website_new = website
            entry.save()
            return redirect(
                'admin_dashboard:website_changelog', website_id=website.id)
    else:
        form = SiteChangelogForm(initial={'website_new': website})

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=form,
        mode='add',
        preset_client=website,
        form_action=reverse(
            'admin_dashboard:changelog_add_website', args=[website.id]),
        clients=_selectable_websites(),
    ))


@admin_required
def changelog_add(request):
    """Add a changelog entry (global — pick the website in the form)."""
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
        clients=_selectable_websites(),
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
    from clients.account_models import Website
    from clients.models import SiteChangelogEntry
    from .forms import SiteChangelogForm

    raw_log = request.POST.get('raw_log', '')
    client_id = request.POST.get('import_client', '')
    step = request.POST.get('step', 'preview')

    # The operator picks the site directly. This used to take an account
    # and then attach every parsed step to `websites.order_by(created_at)
    # .first()` -- so on a two-site account, a deploy to the newer site
    # was recorded against the older one, permanently and with nothing to
    # indicate it had happened.
    site = None
    if client_id and _is_uuid(client_id):
        site = (Website.objects
                .select_related('account')
                .filter(id=client_id)
                .first())

    parsed = _parse_deploy_log(raw_log)

    if step == 'save' and site and parsed:
        today = timezone.localdate()
        for title in parsed:
            # Imported deploy steps land as an internal audit trail — staff
            # flip individual entries visible to surface them to the client.
            SiteChangelogEntry.objects.create(
                website_new=site,
                change_type='deployment',
                title=title,
                is_client_visible=False,
                date_of_change=today,
            )
        return redirect(
            'admin_dashboard:website_changelog', website_id=site.id)

    import_error = None
    if not parsed:
        import_error = 'No "[n/n]" deploy steps were found in that text.'
    elif not site:
        import_error = 'Choose a website to import these steps into.'

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=SiteChangelogForm(
            initial={'website_new': site} if site else None),
        mode='add',
        preset_client=site,
        form_action=reverse('admin_dashboard:changelog_add'),
        clients=_selectable_websites(),
        import_preview=parsed,
        import_raw=raw_log,
        import_client=site,
        import_error=import_error,
    ))


