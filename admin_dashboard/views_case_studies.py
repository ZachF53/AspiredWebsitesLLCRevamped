"""
Case study admin.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names, so urls.py -- which references them as
`views.<name>` -- keeps working unchanged.

Phase-D cutover: the subject of a case study is a **website**, not an
account. That is not just bookkeeping. An account with two sites (Vance
Family Law and Vance Mediation) has two different stories to tell, and the
old picker offered only the account, so the second site could never be
written up without overwriting the first one's subject. Selecting the site
directly fixes that and removes the last legacy read from this module.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)


def _selectable_websites():
    """Every non-tester website, ordered the way the picker reads."""
    from clients.account_models import Website

    return (Website.objects
            .filter(account__is_tester=False)
            .select_related('account')
            .order_by('account__name', 'name'))


def _website_or_none(raw):
    """Resolve a website id from form input, tolerating junk."""
    from clients.account_models import Website

    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return (Website.objects
                .select_related('account')
                .get(id=raw))
    except (Website.DoesNotExist, ValueError, TypeError):
        return None


def _website_location(website):
    """"City, State" for a website's account, or empty."""
    if website is None or website.account is None:
        return ''
    account = website.account
    return ', '.join(p for p in (account.city, account.state) if p)


def _website_business_type(website):
    """Business type, preferring the site's own over the account default."""
    if website is None:
        return ''
    return website.business_type or ''


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 - Case studies
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def case_studies_list(request):
    """List view of CaseStudy rows."""
    from clients.models import CaseStudy
    case_studies = (CaseStudy.objects
                    .select_related('website_new', 'website_new__account')
                    .order_by('-created_at'))
    return render(request, 'admin_dashboard/case_studies_list.html',
                  _admin_context(
                      'case_studies',
                      case_studies=case_studies,
                  ))


@admin_required
def case_study_new(request):
    """Create a new CaseStudy (form + save)."""
    from clients.models import CaseStudy

    if request.method == 'POST':
        website = _website_or_none(request.POST.get('website_id'))

        is_published = request.POST.get('is_published') == 'on'

        cs = CaseStudy.objects.create(
            # Stays None for a marketing case study with no client attached.
            website_new=website,
            title=(request.POST.get('title') or '').strip()[:300],
            business_type=(request.POST.get('business_type')
                           or _website_business_type(website)
                           or '').strip()[:100],
            location=(request.POST.get('location')
                      or _website_location(website)
                      or '').strip()[:100],
            challenge=(request.POST.get('challenge') or '').strip(),
            solution=(request.POST.get('solution') or '').strip(),
            results=(request.POST.get('results') or '').strip(),
            metric_1_label=(request.POST.get('metric_1_label')
                            or '').strip()[:100],
            metric_1_value=(request.POST.get('metric_1_value')
                            or '').strip()[:50],
            metric_2_label=(request.POST.get('metric_2_label')
                            or '').strip()[:100],
            metric_2_value=(request.POST.get('metric_2_value')
                            or '').strip()[:50],
            metric_3_label=(request.POST.get('metric_3_label')
                            or '').strip()[:100],
            metric_3_value=(request.POST.get('metric_3_value')
                            or '').strip()[:50],
            testimonial_quote=(request.POST.get('testimonial_quote')
                               or '').strip(),
            testimonial_name=(request.POST.get('testimonial_name')
                              or '').strip()[:100],
            is_published=is_published,
            published_at=(timezone.now() if is_published else None),
        )
        return redirect('admin_dashboard:case_study_edit', cs_id=cs.id)

    preselect_website = _website_or_none(request.GET.get('website'))

    return render(request, 'admin_dashboard/case_study_form.html',
                  _admin_context(
                      'case_studies',
                      websites=_selectable_websites(),
                      case_study=None,
                      preselect_website=preselect_website,
                  ))


@admin_required
def case_study_edit(request, cs_id):
    """Edit an existing CaseStudy."""
    from clients.models import CaseStudy

    cs = get_object_or_404(
        CaseStudy.objects.select_related('website_new', 'website_new__account'),
        id=cs_id)

    if request.method == 'POST':
        # An unparseable id keeps the existing subject rather than
        # silently detaching the case study from its site.
        website = _website_or_none(request.POST.get('website_id'))
        if website is None and (request.POST.get('website_id') or '').strip():
            website = cs.website_new

        was_published = cs.is_published
        is_published = request.POST.get('is_published') == 'on'

        cs.website_new = website
        cs.title = (request.POST.get('title') or '').strip()[:300]
        cs.business_type = (request.POST.get('business_type')
                            or '').strip()[:100]
        cs.location = (request.POST.get('location') or '').strip()[:100]
        cs.challenge = (request.POST.get('challenge') or '').strip()
        cs.solution = (request.POST.get('solution') or '').strip()
        cs.results = (request.POST.get('results') or '').strip()
        cs.metric_1_label = (
            request.POST.get('metric_1_label') or '').strip()[:100]
        cs.metric_1_value = (
            request.POST.get('metric_1_value') or '').strip()[:50]
        cs.metric_2_label = (
            request.POST.get('metric_2_label') or '').strip()[:100]
        cs.metric_2_value = (
            request.POST.get('metric_2_value') or '').strip()[:50]
        cs.metric_3_label = (
            request.POST.get('metric_3_label') or '').strip()[:100]
        cs.metric_3_value = (
            request.POST.get('metric_3_value') or '').strip()[:50]
        cs.testimonial_quote = (
            request.POST.get('testimonial_quote') or '').strip()
        cs.testimonial_name = (
            request.POST.get('testimonial_name') or '').strip()[:100]
        cs.is_published = is_published
        if is_published and not was_published:
            cs.published_at = timezone.now()
        cs.save()
        return redirect('admin_dashboard:case_studies_list')

    return render(request, 'admin_dashboard/case_study_form.html',
                  _admin_context(
                      'case_studies',
                      websites=_selectable_websites(),
                      case_study=cs,
                      preselect_website=cs.website_new,
                  ))


@admin_required
@require_POST
def case_study_toggle_publish(request, cs_id):
    """One-click toggle on the list page."""
    from clients.models import CaseStudy
    cs = get_object_or_404(CaseStudy, id=cs_id)
    cs.is_published = not cs.is_published
    if cs.is_published and not cs.published_at:
        cs.published_at = timezone.now()
    cs.save(update_fields=[
        'is_published', 'published_at', 'updated_at'])
    return redirect('admin_dashboard:case_studies_list')


@admin_required
@require_POST
def case_study_ai_draft(request):
    """
    POST a {website_id, title?} pair, get back a JSON draft of
    challenge / solution / results / 3 metrics. Front-end renders the
    response into the form fields.

    The metric fields come back **empty unless the intake actually
    contained a number**. An earlier version of this prompt told the model
    to "estimate plausible metrics (e.g. '40%' increase in inquiries)" when
    real ones were unavailable. Those drafts feed published case studies on
    the public site, so that instruction was manufacturing client results
    -- invented numbers, attributed to a named real business, presented as
    outcomes. An empty metric box the admin has to fill in from real data
    is the only safe default.
    """
    import json

    from reporting.ai import (
        AIError, AINotConfigured, claude_complete, MODEL_CONTENT,
    )

    raw_id = (request.POST.get('website_id') or '').strip()
    if not raw_id:
        return HttpResponseBadRequest('website_id required')
    website = _website_or_none(raw_id)
    if website is None:
        return HttpResponseBadRequest('website not found')

    title_hint = (request.POST.get('title') or '').strip()

    package_label = (website.get_package_display()
                     if website.package else '')

    intake_summary = ''
    intake = getattr(website, 'intake_new', None)
    if intake is not None:
        bits = [
            intake.about_copy,
            f'Practice areas: {intake.practice_areas}'
            if intake.practice_areas else '',
            f'Brand colors: {intake.brand_colors}'
            if intake.brand_colors else '',
        ]
        intake_summary = '\n'.join(b for b in bits if b)[:1500]

    location = _website_location(website)

    system = (
        "You are writing a case study for Aspired Websites LLC, a "
        "custom web design agency. Keep it concise and focused on "
        "business impact. Avoid hype and clichés. Return ONLY a JSON "
        "object with keys: challenge, solution, results, "
        "metric_1_label, metric_1_value, metric_2_label, "
        "metric_2_value, metric_3_label, metric_3_value. No prose "
        "outside the JSON."
        "\n\n"
        "Never invent a metric. If the supplied information does not "
        "contain a real, measured number, return an empty string for "
        "both that metric's label and its value. A blank field is "
        "correct; a plausible-sounding estimate is not, because these "
        "drafts are published as results achieved for a named client."
    )

    user = (
        f"Website: {website.name}\n"
        f"Account: {website.account.name if website.account else 'unknown'}\n"
        f"Business type: {_website_business_type(website) or 'unspecified'}\n"
        f"Location: {location or 'unspecified'}\n"
        f"Project package: {package_label or 'unspecified'}\n"
        f"Working title: {title_hint or 'not provided'}\n\n"
        f"Available info from their intake:\n{intake_summary or '(none)'}\n\n"
        "Write the case study now. Describe the challenge and the work "
        "honestly from the information above. Leave any metric you "
        "cannot source from that information blank."
    )

    try:
        raw = claude_complete(
            messages=[{'role': 'user', 'content': user}],
            system=system,
            model=MODEL_CONTENT,
            max_tokens=1200,
        )
    except AINotConfigured:
        return HttpResponse(
            'ANTHROPIC_API_KEY not configured.', status=503)
    except AIError as exc:
        return HttpResponse(f'AI draft failed: {exc}', status=502)

    # Defensive JSON parse - strip code fences if Claude adds them.
    stripped = raw.strip()
    if stripped.startswith('```'):
        stripped = stripped.strip('`')
        if stripped.lower().startswith('json'):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return HttpResponse(
            'AI returned non-JSON. Try again.', status=502)

    from django.http import JsonResponse
    return JsonResponse({
        'challenge': data.get('challenge', ''),
        'solution': data.get('solution', ''),
        'results': data.get('results', ''),
        'metric_1_label': data.get('metric_1_label', ''),
        'metric_1_value': data.get('metric_1_value', ''),
        'metric_2_label': data.get('metric_2_label', ''),
        'metric_2_value': data.get('metric_2_value', ''),
        'metric_3_label': data.get('metric_3_label', ''),
        'metric_3_value': data.get('metric_3_value', ''),
    })
