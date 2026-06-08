"""
Onboarding wizard engine views.

Same flow for every product_type — the registry drives what questions
appear, the engine just renders them.

URL structure:
    /onboarding/                          → routes to the user's most
                                            recent in-progress onboarding,
                                            or shows a "nothing in progress"
                                            page if none.
    /onboarding/<pt>/<tier>/welcome/      → welcome screen
    /onboarding/<pt>/<tier>/<section>/    → wizard step
    /onboarding/<pt>/<tier>/complete/     → completion screen
    /onboarding/<pt>/<tier>/save/         → AJAX save (POST)
    /onboarding/<pt>/<tier>/skip/         → AJAX skip (POST)
"""

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Onboarding,
    OnboardingResponse,
    PRODUCT_TYPE_CHOICES,
)
from .registry import (
    SOCIAL_TIER_CHANNELS,
    find_section,
    total_visible_questions,
    visible_sections,
)


# Estimated time per product_type (Section 9 of the spec)
TIME_ESTIMATES = {
    ('maintenance',    'no_moveover'): 12,
    ('maintenance',    'moveover'):    18,
    ('social_media',   'social-basic'): 15,
    ('social_media',   'social-standard'): 22,
    ('social_media',   'social-full'): 30,
    ('website_design', 'any'): 15,
}


def _estimate_minutes(onboarding):
    """Pick the right time estimate for a product+tier combo."""
    pt = onboarding.product_type
    if pt == 'maintenance':
        from onboarding.registry import _has_hosting_moveover
        key = 'moveover' if _has_hosting_moveover(onboarding.user) else 'no_moveover'
        return TIME_ESTIMATES.get((pt, key), 12)
    if pt == 'social_media':
        return TIME_ESTIMATES.get((pt, onboarding.tier_slug), 22)
    return TIME_ESTIMATES.get((pt, 'any'), 15)


def _progress(onboarding):
    """
    Return a dict {percent, answered, skipped, total} for the
    progress bar. Both answered AND skipped count toward percent.
    """
    total = total_visible_questions(onboarding)
    if total == 0:
        return {'percent': 0, 'answered': 0, 'skipped': 0, 'total': 0}
    answered = onboarding.responses.filter(skipped=False).exclude(value='').count()
    skipped = onboarding.responses.filter(skipped=True).count()
    pct = round((answered + skipped) * 100 / total)
    return {
        'percent': pct,
        'answered': answered,
        'skipped': skipped,
        'total': total,
    }


def _breadcrumbs(onboarding, current_key):
    """List of {key, title, state} for the section breadcrumb strip."""
    sections = visible_sections(onboarding)
    completed_keys = set()
    for sec in sections:
        keys = {q['key'] for q in sec['questions']}
        if not keys:
            continue
        answered = onboarding.responses.filter(
            question_key__in=keys).count()
        if answered == len(keys):
            completed_keys.add(sec['key'])

    out = []
    seen_current = False
    for sec in sections:
        if sec['key'] == current_key:
            state = 'current'
            seen_current = True
        elif sec['key'] in completed_keys:
            state = 'done'
        elif seen_current:
            state = 'upcoming'
        else:
            state = 'upcoming'
        out.append({
            'key': sec['key'],
            'title': sec['title'],
            'state': state,
        })
    return out


# ── Routing entry point ────────────────────────────────────────────

@login_required
def dispatch(request):
    """
    /onboarding/ → find the user's most recent in-progress onboarding
    and redirect there. If none → show empty-state page.
    """
    ob = (Onboarding.objects
          .filter(user=request.user, completed_at__isnull=True)
          .order_by('-started_at')
          .first())
    if ob is None:
        return render(request, 'onboarding/empty.html')
    if not ob.welcome_seen:
        return redirect('onboarding:welcome',
                        product_type=ob.product_type, tier_slug=ob.tier_slug)
    # Resume at last visited section, or first section if none yet
    target = ob.last_section
    if not target:
        secs = visible_sections(ob)
        target = secs[0]['key'] if secs else None
    if target is None:
        return redirect('onboarding:complete',
                        product_type=ob.product_type, tier_slug=ob.tier_slug)
    return redirect('onboarding:step',
                    product_type=ob.product_type, tier_slug=ob.tier_slug,
                    section_key=target)


# ── Per-product views ──────────────────────────────────────────────

@login_required
def welcome(request, product_type, tier_slug):
    ob = get_object_or_404(
        Onboarding, user=request.user,
        product_type=product_type, tier_slug=tier_slug,
    )
    secs = visible_sections(ob)
    if request.method == 'POST':
        ob.welcome_seen = True
        ob.save(update_fields=['welcome_seen'])
        first = secs[0]['key'] if secs else None
        if first is None:
            return redirect('onboarding:complete',
                            product_type=product_type, tier_slug=tier_slug)
        return redirect('onboarding:step',
                        product_type=product_type, tier_slug=tier_slug,
                        section_key=first)

    return render(request, 'onboarding/welcome.html', {
        'onboarding': ob,
        'sections': secs,
        'total_questions': total_visible_questions(ob),
        'estimate_minutes': _estimate_minutes(ob),
    })


@login_required
def step(request, product_type, tier_slug, section_key):
    ob = get_object_or_404(
        Onboarding, user=request.user,
        product_type=product_type, tier_slug=tier_slug,
    )
    section = find_section(ob, section_key)
    if section is None:
        # Bad section — bounce to dispatch
        return redirect('onboarding:dispatch')

    # Bookmark resume position
    ob.last_section = section_key
    ob.save(update_fields=['last_section'])

    secs = visible_sections(ob)
    section_index = next(
        (i for i, s in enumerate(secs) if s['key'] == section_key), 0)
    prev_section = secs[section_index - 1]['key'] if section_index > 0 else None
    next_section = (secs[section_index + 1]['key']
                    if section_index + 1 < len(secs) else None)

    # Pre-fill question values + skipped flags from existing responses
    existing = {
        r.question_key: r
        for r in ob.responses.filter(
            question_key__in=[q['key'] for q in section['questions']])
    }
    questions = []
    for q in section['questions']:
        r = existing.get(q['key'])
        questions.append({
            **q,
            'value': r.value if r else '',
            'skipped': r.skipped if r else False,
        })

    return render(request, 'onboarding/step.html', {
        'onboarding': ob,
        'section': section,
        'section_index': section_index + 1,  # 1-based for display
        'section_count': len(secs),
        'questions': questions,
        'breadcrumbs': _breadcrumbs(ob, section_key),
        'progress': _progress(ob),
        'prev_section': prev_section,
        'next_section': next_section,
        'is_last_section': next_section is None,
    })


@login_required
@require_POST
def save_answer(request, product_type, tier_slug):
    """AJAX — single question save. Body: {question_key, value}."""
    ob = get_object_or_404(
        Onboarding, user=request.user,
        product_type=product_type, tier_slug=tier_slug,
    )
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse('bad json', status=400)

    key = (payload.get('question_key') or '').strip()
    value = (payload.get('value') or '').strip()
    if not key:
        return HttpResponse('missing question_key', status=400)

    # Validate the key actually exists in this product_type's registry
    valid_keys = set()
    for sec in visible_sections(ob):
        valid_keys.update(q['key'] for q in sec['questions'])
    if key not in valid_keys:
        return HttpResponse('unknown question_key', status=400)

    OnboardingResponse.objects.update_or_create(
        onboarding=ob, question_key=key,
        defaults={'value': value, 'skipped': False},
    )
    return JsonResponse({'ok': True, 'progress': _progress(ob)})


@login_required
@require_POST
def skip_answer(request, product_type, tier_slug):
    """AJAX — mark a single question explicitly skipped."""
    ob = get_object_or_404(
        Onboarding, user=request.user,
        product_type=product_type, tier_slug=tier_slug,
    )
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse('bad json', status=400)
    key = (payload.get('question_key') or '').strip()
    if not key:
        return HttpResponse('missing question_key', status=400)

    valid_keys = set()
    for sec in visible_sections(ob):
        for q in sec['questions']:
            if q['key'] == key and q.get('skip_allowed', True):
                valid_keys.add(q['key'])
    if key not in valid_keys:
        return HttpResponse('skip not allowed for this question', status=400)

    OnboardingResponse.objects.update_or_create(
        onboarding=ob, question_key=key,
        defaults={'value': '', 'skipped': True},
    )
    return JsonResponse({'ok': True, 'progress': _progress(ob)})


@login_required
def complete(request, product_type, tier_slug):
    """
    Final screen — marks the Onboarding completed if not already, then
    shows the "what's next" page and lets the user jump to their
    SetupTodo list.
    """
    ob = get_object_or_404(
        Onboarding, user=request.user,
        product_type=product_type, tier_slug=tier_slug,
    )
    if ob.completed_at is None:
        ob.completed_at = timezone.now()
        ob.save(update_fields=['completed_at'])
        # Phase 3 will hook in here to create SetupTodos from the
        # responses. For now, just log.
        try:
            from onboarding.todo_models import build_todos_from_onboarding
            build_todos_from_onboarding(ob)
        except Exception:
            pass

    return render(request, 'onboarding/complete.html', {
        'onboarding': ob,
        'progress': _progress(ob),
    })


# ── SetupTodo widget views ─────────────────────────────────────────

@login_required
def todo_modal(request):
    """HTMX partial — list of open + completed SetupTodos for the user."""
    from .todo_models import SetupTodo
    pending = SetupTodo.objects.filter(
        user=request.user, status='pending')
    completed = SetupTodo.objects.filter(
        user=request.user, status='completed').order_by('-completed_at')[:25]
    return render(request, 'onboarding/_todo_modal.html', {
        'pending': pending,
        'completed': completed,
        'pending_count': pending.count(),
    })


@login_required
def todo_count(request):
    """JSON endpoint for the sidebar badge — { count: N }."""
    from .todo_models import SetupTodo
    n = SetupTodo.objects.filter(
        user=request.user, status='pending').count()
    return JsonResponse({'count': n})
