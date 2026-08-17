"""
Monthly reports, freshness, NPS, blog and chatbot admin.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

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
import json
from .utils import _is_uuid

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Phase 5b — monthly reports, freshness, NPS, blog, chatbot
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def reports_list(request):
    """All monthly reports, with client/status filters + a generate form."""
    from clients.models import ClientProfile
    from reporting.models import MonthlyReport

    reports = MonthlyReport.objects.select_related('client')
    client_filter = request.GET.get('client', '')
    status_filter = request.GET.get('status', '')
    if client_filter and _is_uuid(client_filter):
        reports = reports.filter(client_id=client_filter)
    if status_filter:
        reports = reports.filter(status=status_filter)

    return render(request, 'admin_dashboard/reports_list.html', _admin_context(
        'reports',
        reports=reports,
        clients=ClientProfile.objects.order_by('firm_name'),
        statuses=MonthlyReport.STATUS_CHOICES,
        client_filter=client_filter,
        status_filter=status_filter,
        done=request.GET.get('done', ''),
    ))


@admin_required
@require_POST
def report_generate_now(request):
    """Generate (and send) one client's monthly report immediately."""
    from datetime import date

    from clients.models import ClientProfile
    from reporting.tasks import generate_monthly_report

    client_id = request.POST.get('client', '')
    if not _is_uuid(client_id) or not ClientProfile.objects.filter(
            id=client_id).exists():
        return redirect('admin_dashboard:reports_list')
    try:
        month = date.fromisoformat(
            request.POST.get('report_month', '')).replace(day=1)
    except (ValueError, TypeError):
        today = timezone.localdate()
        month = (date(today.year - 1, 12, 1) if today.month == 1
                 else date(today.year, today.month - 1, 1))
    generate_monthly_report(client_id, month.isoformat())
    return redirect(f"{reverse('admin_dashboard:reports_list')}?done=1")


@admin_required
@require_POST
def report_resend(request, report_id):
    """Re-send an already-generated monthly report."""
    from reporting.models import MonthlyReport
    from reporting.tasks import send_monthly_report_email
    report = get_object_or_404(MonthlyReport, id=report_id)
    send_monthly_report_email(report)
    return redirect(f"{reverse('admin_dashboard:reports_list')}?done=1")


@admin_required
def report_download(request, report_id):
    """Download a monthly report's generated file."""
    import os

    from django.http import FileResponse, Http404

    from reporting.models import MonthlyReport
    report = get_object_or_404(MonthlyReport, id=report_id)
    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path or '')
    if not report.pdf_path or not os.path.exists(abs_path):
        raise Http404('Report file not found.')
    return FileResponse(
        open(abs_path, 'rb'), as_attachment=True,
        filename=os.path.basename(abs_path))


@admin_required
def website_freshness(request, website_id):
    """Content-freshness report for one website."""
    from clients.account_models import Website
    from reporting.models import ContentFreshnessReport

    website = get_object_or_404(Website, id=website_id)
    reports = ContentFreshnessReport.objects.filter(website_new=website)
    report_id = request.GET.get('report', '')
    report = (reports.filter(id=report_id).first() if _is_uuid(report_id)
              else reports.first())
    return render(request, 'admin_dashboard/client_freshness.html',
                  _admin_context(
                      'clients', website=website, report=report,
                      previous_reports=list(reports[:12])))


@admin_required
@require_POST
def freshness_generate(request, website_id):
    """Run a freshness crawl for one website on demand."""
    from clients.account_models import Website
    from reporting.tasks import generate_freshness_report
    website = get_object_or_404(Website, id=website_id)
    # The crawl task still keys off the legacy profile (it reads the live
    # URL there) and stamps website_new on the report it writes.
    cp = website.account.legacy_client_profile
    if cp is not None:
        generate_freshness_report(str(cp.id))
    return redirect('admin_dashboard:website_freshness', website_id=website.id)


@admin_required
@require_POST
def freshness_flag(request, website_id):
    """Flag a stale page — logs an internal-only changelog entry."""
    from clients.account_models import Website
    from clients.models import SiteChangelogEntry
    website = get_object_or_404(Website, id=website_id)
    url = (request.POST.get('url') or '').strip()
    title = (request.POST.get('title') or '').strip()
    SiteChangelogEntry.objects.create(
        client=website.account.legacy_client_profile,
        website_new=website,
        change_type='content_update',
        title=f'Content flagged for update: {title or url}'[:200],
        description=f'Flagged from the content freshness report.\n{url}',
        is_client_visible=False,
        url_changed=url[:200],
    )
    return redirect('admin_dashboard:website_freshness', website_id=website.id)


@admin_required
def nps_list(request):
    """All NPS responses across clients, with a score-band filter."""
    from reporting.models import NPSSurvey

    surveys = NPSSurvey.objects.select_related('client')
    band = request.GET.get('band', '')
    if band == 'promoter':
        surveys = surveys.filter(score__gte=9)
    elif band == 'passive':
        surveys = surveys.filter(score__gte=7, score__lte=8)
    elif band == 'detractor':
        surveys = surveys.filter(score__lte=6, score__isnull=False)
    elif band == 'no_response':
        surveys = surveys.filter(score__isnull=True)

    responded = NPSSurvey.objects.exclude(score__isnull=True)
    avg = responded.aggregate(a=Avg('score'))['a']
    return render(request, 'admin_dashboard/nps_list.html', _admin_context(
        'nps',
        surveys=list(surveys[:200]),
        band=band,
        avg_score=round(avg, 1) if avg is not None else None,
        response_count=responded.count(),
    ))


# ── AI blog generator ───────────────────────────────────────────────────────

_BLOG_WORD_TARGETS = {'short': 500, 'medium': 800, 'long': 1200}


def _blog_system_prompt(client, topic, keyword, length, tone):
    """The system prompt for AI blog generation."""
    from reporting.ai import client_location_phrase

    words = _BLOG_WORD_TARGETS.get(length, 800)
    biz = client.business_type or 'business'
    keyword_line = (
        f'- Naturally include the target keyword "{keyword}" 3-5 times\n'
        if keyword else '')
    return (
        f'You are an expert content writer specializing in {biz} SEO. Write a '
        f'blog post for {client.firm_name}, a {biz}'
        f'{client_location_phrase(client)}.\n\n'
        f'Topic: {topic}\n'
        f'Target keyword: {keyword or "(none specified)"}\n'
        f'Length: approximately {words} words\n'
        f'Tone: {tone}\n\n'
        'The post should:\n'
        '- Be informative and helpful to potential clients\n'
        f'{keyword_line}'
        f'- Include a clear call to action at the end mentioning '
        f'{client.firm_name}\n'
        '- Be formatted as clean HTML with proper heading tags (h2, h3), '
        'paragraph tags, and a bulleted list where appropriate\n'
        '- Start with an engaging introduction\n'
        f'- End with: contact {client.firm_name} at '
        f'{client.phone or "our office"} for a free consultation\n\n'
        'Return ONLY the HTML content — no explanations, no markdown fences.'
    )


def _generate_blog_content(post, length, tone):
    """Run AI generation, populating post.title / content / meta_description."""
    import re as _re

    from django.utils.html import strip_tags

    from reporting.ai import MODEL_CONTENT, claude_complete

    content = claude_complete(
        [{'role': 'user',
          'content': f'Write the blog post about: {post.topic}'}],
        system=_blog_system_prompt(
            post.client, post.topic, post.target_keyword, length, tone),
        model=MODEL_CONTENT, max_tokens=4000,
    )
    content = content.replace('```html', '').replace('```', '').strip()

    meta = claude_complete(
        [{'role': 'user', 'content': (
            f'Write a 155-character meta description for a blog post. '
            f'Topic: {post.topic}. '
            + (f'Include the keyword: {post.target_keyword}. '
               if post.target_keyword else '')
            + 'Return only the meta description text, nothing else.')}],
        model=MODEL_CONTENT, max_tokens=120,
    )

    post.content = content
    post.meta_description = meta.strip()[:160]
    post.word_count = len(strip_tags(content).split())
    heading = _re.search(r'<h[12][^>]*>(.*?)</h[12]>', content,
                         _re.IGNORECASE | _re.DOTALL)
    post.title = (strip_tags(heading.group(1)).strip()[:300]
                  if heading else post.topic[:300])


@admin_required
def blog_list(request):
    """All AI blog posts across clients, with client/status filters."""
    from clients.models import ClientProfile
    from reporting.models import BlogPost

    posts = BlogPost.objects.select_related('client')
    client_filter = request.GET.get('client', '')
    status_filter = request.GET.get('status', '')
    if client_filter and _is_uuid(client_filter):
        posts = posts.filter(client_id=client_filter)
    if status_filter:
        posts = posts.filter(status=status_filter)
    return render(request, 'admin_dashboard/blog_list.html', _admin_context(
        'blog',
        posts=posts,
        clients=ClientProfile.objects.order_by('firm_name'),
        statuses=BlogPost.STATUS_CHOICES,
        client_filter=client_filter,
        status_filter=status_filter,
    ))


@admin_required
def blog_generate(request):
    """The AI blog post generator form."""
    from reporting.ai import AIError, AINotConfigured, is_configured
    from reporting.models import BlogPost

    from .forms import BlogGenerateForm

    if request.method == 'POST':
        form = BlogGenerateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            post = BlogPost(
                client=cd['client'], topic=cd['topic'],
                target_keyword=cd['target_keyword'],
                requested_length=cd['length'], requested_tone=cd['tone'],
                status='review', generated_by_ai=True)
            try:
                _generate_blog_content(post, cd['length'], cd['tone'])
            except AINotConfigured:
                form.add_error(None, 'ANTHROPIC_API_KEY is not configured — '
                                     'set it before generating posts.')
            except AIError as exc:
                form.add_error(None, f'AI generation failed: {exc}')
            else:
                post.save()
                return redirect('admin_dashboard:blog_detail', post_id=post.id)
    else:
        form = BlogGenerateForm()
    return render(request, 'admin_dashboard/blog_generate.html', _admin_context(
        'blog', form=form, ai_ready=is_configured(),
    ))


@admin_required
def blog_detail(request, post_id):
    """Review / edit one blog post and run its workflow actions."""
    from django.utils.html import strip_tags

    from reporting.ai import AIError
    from reporting.models import BlogPost

    post = get_object_or_404(BlogPost, id=post_id)
    error = None

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        post.title = (request.POST.get('title') or post.title)[:300]
        post.meta_description = (request.POST.get('meta_description') or '')[:160]
        post.content = request.POST.get('content') or post.content
        post.word_count = len(strip_tags(post.content).split())

        if action == 'approve':
            post.status = 'approved'
            post.reviewed_by = request.user.get_username()
            post.reviewed_at = timezone.now()
        elif action == 'reject':
            post.status = 'rejected'
        elif action == 'publish':
            post.published_url = (request.POST.get('published_url') or '')[:200]
            post.status = 'published'
            post.published_at = timezone.now()
        elif action == 'regenerate':
            try:
                _generate_blog_content(
                    post, post.requested_length or 'medium',
                    post.requested_tone or 'professional')
                post.status = 'review'
            except AIError as exc:
                error = f'Regeneration failed: {exc}'

        post.save()
        if not error:
            return redirect('admin_dashboard:blog_detail', post_id=post.id)

    return render(request, 'admin_dashboard/blog_detail.html', _admin_context(
        'blog', post=post, error=error,
    ))


# ── AI chatbot configuration ────────────────────────────────────────────────

def _chatbot_for_website(website):
    """Resolve (or create) the ClientChatbot for a Website. Falls back to the
    account's existing chatbot (client is O2O, so multi-website accounts share
    one config until the FK flip)."""
    from reporting.models import ClientChatbot
    bot = ClientChatbot.objects.filter(website_new=website).first()
    if bot is not None:
        return bot
    cp = website.account.legacy_client_profile
    if cp is None:
        return None
    bot = ClientChatbot.objects.filter(client=cp).first()
    if bot is None:
        bot = ClientChatbot.objects.create(client=cp, website_new=website)
    return bot


@admin_required
def website_chatbot(request, website_id):
    """Configure a website's AI chatbot."""
    from clients.account_models import Website

    from .forms import ChatbotConfigForm

    website = get_object_or_404(Website, id=website_id)
    chatbot = _chatbot_for_website(website)

    if request.method == 'POST':
        form = ChatbotConfigForm(request.POST, instance=chatbot)
        if form.is_valid():
            form.save()
            return redirect(
                'admin_dashboard:website_chatbot', website_id=website.id)
    else:
        form = ChatbotConfigForm(instance=chatbot)

    snippet = (
        f'<script src="{settings.SITE_BASE_URL}/static/js/aspired-chat.js" '
        f'data-aspired-client="{chatbot.client_id}" defer></script>'
    )
    return render(request, 'admin_dashboard/client_chatbot.html', _admin_context(
        'clients',
        website=website,
        chatbot=chatbot,
        form=form,
        snippet=snippet,
        conversations=list(chatbot.conversations.all()[:20]),
    ))


@admin_required
@require_POST
def chatbot_regenerate_prompt(request, website_id):
    """Use Claude to write a system prompt from the client's info + FAQs."""
    from clients.account_models import Website
    from reporting.ai import MODEL_CONTENT, AIError, claude_complete

    website = get_object_or_404(Website, id=website_id)
    chatbot = _chatbot_for_website(website)
    client = chatbot.client

    project = client    # ← alias post-2026-05-25 refactor (project fields on ClientProfile)
    intake = getattr(project, 'intake', None) if project else None
    practice_areas = getattr(intake, 'practice_areas', '') or ''
    raw = (
        f'Business: {client.firm_name}\n'
        f'Type: {client.business_type or "law firm"}\n'
        f'Phone: {client.phone or "(not set)"}\n'
        f'Practice areas / services: {practice_areas or "(not provided)"}\n'
        f'FAQ notes:\n{chatbot.faq_text or "(none provided)"}'
    )
    try:
        prompt = claude_complete(
            [{'role': 'user', 'content': (
                'Write a concise, professional system prompt (3-6 sentences) '
                'for an AI website chatbot, based on the business info below. '
                'Describe what the bot helps visitors with and the key facts '
                'it should know. Return only the prompt text.\n\n' + raw)}],
            model=MODEL_CONTENT, max_tokens=500,
        )
        chatbot.system_prompt = prompt
        chatbot.save(update_fields=['system_prompt', 'updated_at'])
    except AIError:
        logger.exception('Chatbot prompt regeneration failed')
    return redirect('admin_dashboard:website_chatbot', website_id=website.id)


@admin_required
def chatbot_conversation(request, website_id, conv_id):
    """Full transcript of one chatbot conversation."""
    from clients.account_models import Website
    from reporting.models import ChatbotConversation

    website = get_object_or_404(Website, id=website_id)
    chatbot = _chatbot_for_website(website)
    conversation = get_object_or_404(
        ChatbotConversation, id=conv_id, chatbot=chatbot)
    return render(request, 'admin_dashboard/chatbot_conversation.html',
                  _admin_context(
                      'clients', website=website, conversation=conversation))


@admin_required
@require_POST
def testimonial_mark_received(request, client_id):
    """Record a received video testimonial against a client."""
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    client.testimonial_received = True
    client.testimonial_url = (request.POST.get('testimonial_url') or '')[:200]
    client.save(update_fields=[
        'testimonial_received', 'testimonial_url', 'updated_at'])
    return redirect('admin_dashboard:client_detail', client_id=client.id)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_droplets.py
# ──────────────────────────────────────────────────────────────────────────
from .views_droplets import (  # noqa: E402,F401
    _droplet_rows,
    _fetch_ssh_metrics,
    _load_droplet_dashboard,
    droplet_destroy,
    droplet_link_to_website,
    droplet_list,
    droplet_metrics,
    droplet_new,
    droplet_power,
    droplet_table,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_scans.py
# ──────────────────────────────────────────────────────────────────────────
from .views_scans import (  # noqa: E402,F401
    _build_scan_rows,
    _build_tool_blocks,
    _format_duration,
    _scan_row_border,
    _ssl_grade_class,
    download_scan_pdf,
    generate_scan_pdf_view,
    run_scan,
    scan_cancel,
    scan_detail,
    scans_list,
    scans_table,
    send_scan_report,
    toggle_auto_send_scans,
    update_finding_status,
)


