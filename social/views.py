"""
Phase 5b/5c — Social media manager admin views.

Five pages:
    channels_list  /admin-dashboard/social/
        Cross-client triage. Lists every SocialChannel + connection
        state + scheduled posts pending.

    connect_page   /admin-dashboard/social/<channel_id>/connect/
        Per-channel connect / disconnect page. Dispatches by
        SocialChannel.platform to Meta or LinkedIn OAuth start view.

    post_composer  /admin-dashboard/social/<channel_id>/compose/
        Draft + schedule a post. ?ai=1 fills the body via
        social.ai.generate_post_draft.

    posts_list     /admin-dashboard/social/<channel_id>/posts/
        Per-channel post history with PostResult outcomes.

Tier-gated: only ClientProfiles with an ACTIVE SocialMediaPlan (or a
comp_package that includes social) see SocialChannels in the picker.
"""

import datetime as _dt
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from clients.service_models import SocialChannel, SocialMediaPlan

from .ai import generate_post_draft
from .models import ScheduledPost, SocialToken

logger = logging.getLogger(__name__)


# Platforms wired for actual publishing in Phase 5b/5c. Other channels
# are tracked but the connect button is disabled with a "coming soon"
# message until a publisher exists for that platform.
WIRED_PLATFORMS = {'facebook', 'instagram', 'linkedin'}


@admin_required
def channels_list(request):
    """Cross-client list of SocialChannels with connection state."""
    plans = SocialMediaPlan.objects.filter(
        status='active'
    ).select_related('account').prefetch_related('channels__token')

    rows = []
    for plan in plans:
        for channel in plan.channels.all():
            token = getattr(channel, 'token', None)
            connected = token is not None and bool(
                token.access_token_encrypted)
            pending = ScheduledPost.objects.filter(
                channel=channel,
                status__in=('draft', 'scheduled'),
            ).count()
            rows.append({
                'channel': channel,
                'plan': plan,
                'connected': connected,
                'token': token,
                'pending': pending,
                'wired': channel.platform in WIRED_PLATFORMS,
            })

    return render(request, 'social/channels_list.html', {
        'rows': rows,
        'active': 'social',
    })


def _resolve_account(channel):
    """The Account whose social plan owns this channel, or None.

    This used to take one more hop, to `account.legacy_client_profile`,
    and every composer view refused to work when that came back None --
    which is the shape of every account created after the cutover. The
    Account is the owner; the legacy profile was only ever a passenger.
    """
    return getattr(channel.plan, 'account', None)


@admin_required
def connect_page(request, channel_id):
    """Per-channel connect/disconnect. Shows state + connect button or
    'change account' / 'disconnect'."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    client = _resolve_account(channel)
    token = getattr(channel, 'token', None)
    connected = token is not None and bool(token.access_token_encrypted)
    wired = channel.platform in WIRED_PLATFORMS

    return render(request, 'social/connect.html', {
        'channel': channel,
        'client': client,
        'token': token,
        'connected': connected,
        'wired': wired,
        'active': 'social',
    })


@admin_required
def post_composer(request, channel_id):
    """Draft / schedule a post for `channel`.

    GET   shows the form
    GET   ?ai=1&prompt=...  populates body via generate_post_draft
    POST  body + scheduled_for -> create ScheduledPost(status='scheduled')
    """
    channel = get_object_or_404(SocialChannel, id=channel_id)
    client = _resolve_account(channel)
    if client is None:
        messages.error(
            request,
            'This channel is not linked to an account. '
            'Attach its social plan to an account first.')
        return redirect('social:channels_list')

    if channel.platform not in WIRED_PLATFORMS:
        messages.error(
            request,
            f'{channel.get_platform_display()} publishing is not yet '
            f'wired. Pick Facebook, Instagram, or LinkedIn for now.')
        return redirect('social:channels_list')

    token = getattr(channel, 'token', None)
    if token is None or not token.access_token_encrypted:
        messages.error(
            request,
            f'Connect {channel.get_platform_display()} before composing '
            f'a post.')
        return redirect('social:connect_page', channel_id=channel.id)

    initial_body = ''
    ai_error = ''
    if request.method == 'GET' and request.GET.get('ai') == '1':
        prompt = (request.GET.get('prompt') or '').strip()
        if not prompt:
            ai_error = 'Add ?prompt=... to generate.'
        else:
            try:
                initial_body = generate_post_draft(
                    client, channel.platform, prompt,
                    tone=request.GET.get('tone') or 'friendly',
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception('AI draft failed')
                ai_error = str(exc)

    if request.method == 'POST':
        body = (request.POST.get('body') or '').strip()
        media_url = (request.POST.get('media_url') or '').strip()
        scheduled_raw = request.POST.get('scheduled_for') or ''
        publish_now = request.POST.get('publish_now') == '1'

        if not body:
            messages.error(request, 'Post body is required.')
            return render(request, 'social/compose.html', {
                'channel': channel, 'client': client,
                'initial_body': body, 'ai_error': '',
                'active': 'social',
            })

        # Parse "YYYY-MM-DDTHH:MM" from datetime-local input
        scheduled_for = None
        if scheduled_raw and not publish_now:
            try:
                naive = _dt.datetime.fromisoformat(scheduled_raw)
                scheduled_for = timezone.make_aware(naive)
            except ValueError:
                messages.error(
                    request,
                    'Scheduled time is invalid — use the date/time picker.')
                return render(request, 'social/compose.html', {
                    'channel': channel, 'client': client,
                    'initial_body': body, 'ai_error': '',
                    'active': 'social',
                })

        if publish_now:
            scheduled_for = timezone.now()

        if scheduled_for is None:
            messages.error(
                request,
                'Pick a scheduled time or check "publish now".')
            return render(request, 'social/compose.html', {
                'channel': channel, 'client': client,
                'initial_body': body, 'ai_error': '',
                'active': 'social',
            })

        post = ScheduledPost.objects.create(
            channel=channel,
            account_new=client,
            body=body,
            media_url=media_url,
            scheduled_for=scheduled_for,
            status='scheduled',
            ai_generated=bool(request.POST.get('was_ai') == '1'),
            created_by=request.user,
        )
        messages.success(
            request,
            f'Post scheduled for '
            f'{scheduled_for.strftime("%Y-%m-%d %H:%M")}.')
        return redirect('social:posts_list', channel_id=channel.id)

    return render(request, 'social/compose.html', {
        'channel': channel,
        'client': client,
        'initial_body': initial_body,
        'ai_error': ai_error,
        'ai_prompt': request.GET.get('prompt', ''),
        'ai_tone': request.GET.get('tone', 'friendly'),
        'was_ai': bool(initial_body and not ai_error),
        'active': 'social',
    })


@admin_required
def posts_list(request, channel_id):
    """Per-channel post history."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    posts = ScheduledPost.objects.filter(channel=channel) \
        .prefetch_related('results') \
        .order_by('-scheduled_for', '-created_at')[:200]

    return render(request, 'social/posts_list.html', {
        'channel': channel,
        'posts': posts,
        'active': 'social',
    })


@admin_required
def post_delete(request, channel_id, post_id):
    """Cancel a draft / scheduled post. Cannot cancel published posts."""
    if request.method != 'POST':
        return redirect('social:posts_list', channel_id=channel_id)
    channel = get_object_or_404(SocialChannel, id=channel_id)
    post = get_object_or_404(ScheduledPost, id=post_id, channel=channel)
    if post.status in ('published', 'publishing'):
        messages.error(
            request,
            'Cannot cancel a post that has already been published or '
            'is publishing right now.')
        return redirect('social:posts_list', channel_id=channel.id)
    post.delete()
    messages.success(request, 'Scheduled post cancelled.')
    return redirect('social:posts_list', channel_id=channel.id)
