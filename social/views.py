"""
Phase 5a — Admin views for the GBP social manager.

Five admin-gated views:
    channels_list(request)            entry point — lists all
                                      SocialChannels grouped by account.
    connect_page(request, channel_id) status + Connect / Disconnect for
                                      one channel. Links into the OAuth
                                      flow in social.google_oauth_views.
    locations_picker(request, channel_id)
                                      Once connected, pick the GBP
                                      location to map the channel to.
    post_composer(request, channel_id)
                                      GET → form; POST → save draft or
                                      schedule. `?ai=1` POST runs
                                      social.ai.generate_post_draft and
                                      pre-fills the body.
    posts_list(request, channel_id)   See all draft / scheduled /
                                      published / failed posts for a
                                      channel.

Dark-theme; mirrors the .admin-card pattern from changelog_add.html.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from admin_dashboard.decorators import admin_required
from clients.service_models import SocialChannel
from social.forms import ComposePostForm
from social.models import ScheduledPost, SocialToken

logger = logging.getLogger(__name__)


@admin_required
def channels_list(request):
    """Top-level — every SocialChannel across every client, grouped
    by account."""
    channels = (SocialChannel.objects
                .select_related('plan', 'plan__account')
                .order_by('plan__account__name', 'platform'))
    by_account = {}
    for ch in channels:
        acct = ch.plan.account
        key = (str(acct.id), acct.name)
        bucket = by_account.setdefault(key, [])
        bucket.append({
            'channel':   ch,
            'connected': SocialToken.objects.filter(channel=ch).exists(),
        })
    groups = [{'account_id': k[0], 'account_name': k[1], 'channels': v}
              for k, v in by_account.items()]
    return render(request, 'social/channels_list.html', {
        'active_nav': 'social',
        'groups':     groups,
    })


@admin_required
def connect_page(request, channel_id):
    """Show OAuth connection state for one channel."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    token = SocialToken.objects.filter(channel=channel).first()
    return render(request, 'social/connect.html', {
        'active_nav': 'social',
        'channel':    channel,
        'token':      token,
        'connected':  token is not None,
    })


@admin_required
def locations_picker(request, channel_id):
    """Once OAuth is done, the operator picks which GBP location this
    channel actually maps to. We POST the location's resource name
    into channel.handle for the publisher to read."""
    from social.google_gbp import list_locations

    channel = get_object_or_404(SocialChannel, id=channel_id)
    token = SocialToken.objects.filter(channel=channel).first()
    if token is None:
        messages.error(request, 'Connect Google first, then pick a location.')
        return redirect('social:connect_page', channel_id=channel.id)

    if request.method == 'POST':
        resource_name = (request.POST.get('location_name') or '').strip()
        if not resource_name.startswith('accounts/'):
            messages.error(request, 'Pick a location from the list.')
            return redirect('social:locations_picker',
                            channel_id=channel.id)
        channel.handle = resource_name
        channel.status = 'active'
        channel.save(update_fields=['handle', 'status', 'updated_at'])
        messages.success(
            request, 'Location bound. You can compose posts now.')
        return redirect('social:posts_list', channel_id=channel.id)

    try:
        locations = list_locations(token)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'locations_picker: GBP list_locations failed for %s',
            channel.id)
        messages.error(
            request, f'Could not fetch GBP locations: {exc}')
        locations = []

    return render(request, 'social/locations_picker.html', {
        'active_nav': 'social',
        'channel':    channel,
        'locations':  locations,
    })


@admin_required
def post_composer(request, channel_id):
    """Draft / schedule a post. `?ai=1` on the POST kicks off AI gen."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    client = _client_for_account(channel.plan.account)
    initial = {}
    ai_used = False
    ai_error = ''

    if request.method == 'POST':
        if request.GET.get('ai') == '1':
            # AI-draft mode: don't validate the form, just generate and
            # bounce back with the body pre-filled.
            from reporting.ai import AIError, AINotConfigured
            from social.ai import generate_post_draft
            topic = (request.POST.get('ai_topic') or '').strip()
            try:
                body = generate_post_draft(client, topic)
            except AINotConfigured:
                ai_error = ('ANTHROPIC_API_KEY is not set on the server. '
                            'Add it to .env and restart gunicorn.')
                body = ''
            except AIError as exc:
                ai_error = f'AI error: {exc}'
                body = ''
            initial = {'ai_topic': topic, 'body': body}
            ai_used = bool(body)
            form = ComposePostForm(initial=initial)
        else:
            form = ComposePostForm(request.POST)
            if form.is_valid():
                cleaned = form.cleaned_data
                status = ('draft' if cleaned.get('save_as_draft')
                          else 'scheduled')
                if client is None:
                    messages.error(
                        request,
                        'This account has no ClientProfile linked — '
                        'cannot create a ScheduledPost without it.')
                    return redirect('social:posts_list',
                                    channel_id=channel.id)
                ScheduledPost.objects.create(
                    channel=channel,
                    client=client,
                    body=cleaned['body'],
                    media_url=cleaned.get('media_url') or '',
                    scheduled_for=cleaned.get('scheduled_for'),
                    status=status,
                    ai_generated=False,
                    created_by=request.user,
                )
                if status == 'scheduled':
                    messages.success(
                        request,
                        'Scheduled. The auto-publisher will pick it up '
                        'within 5 minutes of the target time.')
                else:
                    messages.success(request, 'Draft saved.')
                return redirect('social:posts_list', channel_id=channel.id)
    else:
        form = ComposePostForm()

    return render(request, 'social/compose.html', {
        'active_nav': 'social',
        'channel':    channel,
        'form':       form,
        'ai_used':    ai_used,
        'ai_error':   ai_error,
        'client':     client,
    })


@admin_required
def posts_list(request, channel_id):
    """Show all posts for a channel, ordered newest first."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    posts = (ScheduledPost.objects
             .filter(channel=channel)
             .prefetch_related('results')
             .order_by('-scheduled_for', '-created_at')[:100])
    return render(request, 'social/posts_list.html', {
        'active_nav': 'social',
        'channel':    channel,
        'posts':      posts,
    })


def _client_for_account(account):
    """Resolve an Account to its primary ClientProfile.

    Phase D shipped the Account ↔ ClientProfile linkage; concretely we
    try both directions because the migration state varies row by row.
    Returns None if neither lookup resolves — callers must handle that.
    """
    cp = getattr(account, 'legacy_client_profile', None)
    if cp is not None:
        return cp
    try:
        # Reverse 1:1 from ClientProfile.account → Account
        return account.clientprofile  # type: ignore[attr-defined]
    except Exception:
        return None
