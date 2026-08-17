"""
Onboarding question manager — the database-backed intake builder.

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

# ────────────────────────────────────────────────────────────────────────────
# Onboarding question manager — DB-backed intake builder for all products
# ────────────────────────────────────────────────────────────────────────────

_OB_PRODUCTS = [
    ('maintenance', 'Maintenance'),
    ('social_media', 'Social Media'),
    ('website_design', 'Website Design'),
]


def _parse_choices(raw):
    """Textarea (one `value|Label` per line) → [[value, label], ...]."""
    out = []
    for line in (raw or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if '|' in line:
            v, l = line.split('|', 1)
        else:
            v = l = line
        out.append([v.strip(), l.strip()])
    return out


def _choices_to_text(choices):
    return '\n'.join(
        f"{c[0]}|{c[1]}" if len(c) > 1 else str(c[0])
        for c in (choices or []))


@admin_required
def onboarding_questions(request):
    """List every product's onboarding sections + questions, with the
    manual mark-complete tool."""
    from onboarding.question_models import OnboardingSectionDef

    groups = []
    for key, label in _OB_PRODUCTS:
        secs = list(
            OnboardingSectionDef.objects
            .filter(product_type=key)
            .order_by('sort_order', 'key')
            .prefetch_related('questions'))
        groups.append({'key': key, 'label': label, 'sections': secs})

    return render(
        request, 'admin_dashboard/onboarding_questions.html',
        _admin_context(
            'onboarding_questions',
            groups=groups,
            products=_OB_PRODUCTS,
            saved=request.GET.get('saved'),
            mc_result=request.GET.get('mc'),
        ))


@admin_required
def onboarding_section_form(request, section_id=None):
    """Add / edit a section."""
    from onboarding.question_models import OnboardingSectionDef

    section = (get_object_or_404(OnboardingSectionDef, id=section_id)
               if section_id else None)
    if request.method == 'POST':
        pt = request.POST.get('product_type', '').strip()
        tier_vis = [t.strip() for t in
                    (request.POST.get('tier_visibility') or '').split(',')
                    if t.strip()]
        data = {
            'product_type': pt,
            'key': request.POST.get('key', '').strip(),
            'title': request.POST.get('title', '').strip(),
            'intro': request.POST.get('intro', '').strip(),
            'sort_order': int(request.POST.get('sort_order') or 0),
            'is_active': bool(request.POST.get('is_active')),
            'tier_visibility': tier_vis,
            'requires_hosting_moveover': bool(
                request.POST.get('requires_hosting_moveover')),
            'skip_if_completed_intake': bool(
                request.POST.get('skip_if_completed_intake')),
            'is_channel_template': bool(
                request.POST.get('is_channel_template')),
        }
        if section is None:
            section = OnboardingSectionDef(**data)
        else:
            for k, v in data.items():
                setattr(section, k, v)
        section.save()
        return redirect(
            f"{reverse('admin_dashboard:onboarding_questions')}"
            f"?saved=Section%20{section.key}")

    return render(
        request, 'admin_dashboard/onboarding_section_form.html',
        _admin_context(
            'onboarding_questions',
            section=section,
            products=_OB_PRODUCTS,
            default_product=request.GET.get('product_type', 'maintenance'),
        ))


@admin_required
@require_POST
def onboarding_section_delete(request, section_id):
    from onboarding.question_models import OnboardingSectionDef
    OnboardingSectionDef.objects.filter(id=section_id).delete()
    return redirect(
        f"{reverse('admin_dashboard:onboarding_questions')}?saved=Deleted")


@admin_required
def onboarding_question_form(request, question_id=None):
    """Add / edit a question."""
    from onboarding.question_models import (
        OnboardingQuestionDef, OnboardingSectionDef)

    question = (get_object_or_404(OnboardingQuestionDef, id=question_id)
                if question_id else None)
    section = None
    if question is not None:
        section = question.section
    else:
        section = get_object_or_404(
            OnboardingSectionDef, id=request.GET.get('section'))

    if request.method == 'POST':
        sec_id = request.POST.get('section') or (section.id if section else None)
        target_section = get_object_or_404(OnboardingSectionDef, id=sec_id)
        rows_raw = (request.POST.get('rows') or '').strip()
        data = {
            'section': target_section,
            'key': request.POST.get('key', '').strip(),
            'label': request.POST.get('label', '').strip(),
            'qtype': request.POST.get('qtype', 'text').strip(),
            'help': request.POST.get('help', '').strip(),
            'placeholder': request.POST.get('placeholder', '').strip(),
            'required': bool(request.POST.get('required')),
            'skip_allowed': bool(request.POST.get('skip_allowed')),
            'rows': int(rows_raw) if rows_raw.isdigit() else None,
            'choices': _parse_choices(request.POST.get('choices')),
            'cred_category': request.POST.get('cred_category', '').strip(),
            'cred_type': request.POST.get('cred_type', '').strip(),
            'sort_order': int(request.POST.get('sort_order') or 0),
            'is_active': bool(request.POST.get('is_active')),
        }
        if question is None:
            question = OnboardingQuestionDef(**data)
        else:
            for k, v in data.items():
                setattr(question, k, v)
        question.save()
        return redirect(
            f"{reverse('admin_dashboard:onboarding_questions')}"
            f"?saved=Question%20{question.key}")

    return render(
        request, 'admin_dashboard/onboarding_question_form.html',
        _admin_context(
            'onboarding_questions',
            question=question,
            section=section,
            choices_text=_choices_to_text(question.choices) if question else '',
            qtypes=[
                ('text', 'Short text'), ('textarea', 'Long text'),
                ('select', 'Dropdown'), ('bool', 'Yes / No'),
                ('cred_access', 'Credential access'),
            ],
        ))


@admin_required
@require_POST
def onboarding_question_delete(request, question_id):
    from onboarding.question_models import OnboardingQuestionDef
    OnboardingQuestionDef.objects.filter(id=question_id).delete()
    return redirect(
        f"{reverse('admin_dashboard:onboarding_questions')}?saved=Deleted")


@admin_required
@require_POST
def onboarding_mark_complete(request):
    """Manually mark a user's onboarding(s) for a product complete — for
    legacy clients who never went through the wizard. Never auto-runs."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone as _tz
    from onboarding.models import Onboarding

    email = (request.POST.get('email') or '').strip().lower()
    product_type = (request.POST.get('product_type') or '').strip()
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return redirect(
            f"{reverse('admin_dashboard:onboarding_questions')}"
            f"?mc=No%20user%20with%20that%20email")

    obs = list(Onboarding.objects.filter(
        user=user, product_type=product_type))
    if not obs:
        # Legacy client with no wizard row — create one already complete
        # so the portal shows it done. Derive the tier from their plan.
        tier_slug = _derive_tier_slug(user, product_type)
        ob = Onboarding.objects.create(
            user=user, product_type=product_type, tier_slug=tier_slug,
            welcome_seen=True, completed_at=_tz.now())
        obs = [ob]
    else:
        for ob in obs:
            if ob.completed_at is None:
                ob.completed_at = _tz.now()
                ob.save(update_fields=['completed_at'])
    return redirect(
        f"{reverse('admin_dashboard:onboarding_questions')}"
        f"?mc=Marked%20{len(obs)}%20{product_type}%20onboarding(s)%20"
        f"complete%20for%20{email}")


def _derive_tier_slug(user, product_type):
    """Best-effort tier slug for a manually-created completion row."""
    try:
        from clients.account_models import Account
        acc = Account.objects.filter(user=user).first()
        if acc is None:
            return 'legacy'
        if product_type == 'maintenance':
            p = acc.maintenance_plans.order_by('-started_at').first()
        elif product_type == 'social_media':
            p = acc.social_media_plans.order_by('-started_at').first()
        else:
            p = None
        return (p.tier_slug if p and p.tier_slug else 'legacy')
    except Exception:
        return 'legacy'


