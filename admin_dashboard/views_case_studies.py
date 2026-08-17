"""
Case study admin.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names, so urls.py — which references them as
`views.<name>` — keeps working unchanged.
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


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 — Case studies
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def case_studies_list(request):
    """List view of CaseStudy rows."""
    from clients.models import CaseStudy
    case_studies = (CaseStudy.objects
                    .select_related('client')
                    .order_by('-created_at'))
    return render(request, 'admin_dashboard/case_studies_list.html',
                  _admin_context(
                      'case_studies',
                      case_studies=case_studies,
                  ))


@admin_required
def case_study_new(request):
    """Create a new CaseStudy (form + save)."""
    from clients.models import CaseStudy, ClientProfile

    if request.method == 'POST':
        client = None
        cid = (request.POST.get('client_id') or '').strip()
        if cid:
            try:
                client = ClientProfile.objects.get(id=cid)
            except (ClientProfile.DoesNotExist, ValueError):
                client = None

        is_published = request.POST.get('is_published') == 'on'

        from clients.website_helpers import primary_website
        cs = CaseStudy.objects.create(
            client=client,
            # Stays None for a marketing case study with no client attached.
            website_new=primary_website(client),
            title=(request.POST.get('title') or '').strip()[:300],
            business_type=(request.POST.get('business_type')
                           or (client.business_type if client else '')
                           or '').strip()[:100],
            location=(request.POST.get('location')
                      or _client_location(client)
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

    preselect_client = None
    cid_query = (request.GET.get('client') or '').strip()
    if cid_query:
        try:
            preselect_client = ClientProfile.objects.get(id=cid_query)
        except (ClientProfile.DoesNotExist, ValueError):
            preselect_client = None

    clients = (ClientProfile.objects.filter(is_tester=False)
               .order_by('firm_name'))

    return render(request, 'admin_dashboard/case_study_form.html',
                  _admin_context(
                      'case_studies',
                      clients=clients,
                      case_study=None,
                      preselect_client=preselect_client,
                  ))


@admin_required
def case_study_edit(request, cs_id):
    """Edit an existing CaseStudy."""
    from clients.models import CaseStudy, ClientProfile

    cs = get_object_or_404(CaseStudy, id=cs_id)

    if request.method == 'POST':
        client = cs.client
        cid = (request.POST.get('client_id') or '').strip()
        if cid:
            try:
                client = ClientProfile.objects.get(id=cid)
            except (ClientProfile.DoesNotExist, ValueError):
                pass

        was_published = cs.is_published
        is_published = request.POST.get('is_published') == 'on'

        cs.client = client
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

    clients = (ClientProfile.objects.filter(is_tester=False)
               .order_by('firm_name'))
    return render(request, 'admin_dashboard/case_study_form.html',
                  _admin_context(
                      'case_studies',
                      clients=clients,
                      case_study=cs,
                      preselect_client=cs.client,
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
    POST a {client_id, title?} pair, get back a JSON draft of
    challenge / solution / results / 3 metrics. Front-end renders the
    response into the form fields.
    """
    import json

    from clients.models import ClientProfile
    from reporting.ai import (
        AIError, AINotConfigured, claude_complete, MODEL_CONTENT,
    )

    cid = (request.POST.get('client_id') or '').strip()
    if not cid:
        return HttpResponseBadRequest('client_id required')
    try:
        client = ClientProfile.objects.get(id=cid)
    except (ClientProfile.DoesNotExist, ValueError):
        return HttpResponseBadRequest('client not found')

    title_hint = (request.POST.get('title') or '').strip()

    # Post-2026-05-25 refactor: project fields live on ClientProfile.
    # `project` alias preserved so existing reads (project.stage,
    # project.intake, project.stage_logs, etc.) keep working.
    project = client
    package_label = (project.get_package_display()
                     if project and project.package else '')
    intake_summary = ''
    if project and hasattr(project, 'intake'):
        intake = project.intake
        bits = [
            intake.about_copy,
            f'Practice areas: {intake.practice_areas}'
            if intake.practice_areas else '',
            f'Brand colors: {intake.brand_colors}'
            if intake.brand_colors else '',
        ]
        intake_summary = '\n'.join(b for b in bits if b)[:1500]

    location = _client_location(client)

    system = (
        "You are writing a case study for Aspired Websites LLC, a "
        "custom web design agency. Keep it concise and focused on "
        "business impact. Avoid hype and clichés. Return ONLY a JSON "
        "object with keys: challenge, solution, results, "
        "metric_1_label, metric_1_value, metric_2_label, "
        "metric_2_value, metric_3_label, metric_3_value. No prose "
        "outside the JSON."
    )

    user = (
        f"Client: {client.firm_name}\n"
        f"Business type: {client.business_type or 'unspecified'}\n"
        f"Location: {location or 'unspecified'}\n"
        f"Project package: {package_label or 'unspecified'}\n"
        f"Working title: {title_hint or 'not provided'}\n\n"
        f"Available info from their intake:\n{intake_summary or '(none)'}\n\n"
        "Write the case study now. Estimate plausible metrics (e.g. "
        "'40%' increase in inquiries, '2.3x' faster page load) when "
        "exact numbers are unavailable. Three short metric pairs."
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

    # Defensive JSON parse — strip code fences if Claude adds them.
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


def _client_location(client):
    """City, State string for a client or empty."""
    if not client:
        return ''
    parts = [p for p in (client.city, client.state) if p]
    return ', '.join(parts)


