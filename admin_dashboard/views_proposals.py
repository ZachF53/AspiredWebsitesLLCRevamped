"""
Proposal builder and sending.

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
from django.conf import settings
import datetime


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 — Proposals
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def proposals_list(request):
    """All proposals, newest first."""
    from clients.models import Proposal
    proposals = (Proposal.objects
                 .select_related('lead')
                 .order_by('-created_at'))
    return render(request, 'admin_dashboard/proposals_list.html',
                  _admin_context(
                      'proposals',
                      proposals=proposals,
                  ))


@admin_required
def proposal_new(request):
    """Proposal creation form."""
    from billing.pricing_models import ServiceTier
    from clients.models import CaseStudy, Proposal
    from outreach.models import Lead

    if request.method == 'POST':
        try:
            from decimal import Decimal
            project_price = Decimal(
                request.POST.get('project_price') or '0')
            maintenance_price = Decimal(
                request.POST.get('maintenance_price') or '0')
        except Exception:
            from decimal import Decimal
            project_price = Decimal('0')
            maintenance_price = Decimal('0')

        # Optional Lead link
        lead = None
        lead_id_raw = (request.POST.get('lead_id') or '').strip()
        if lead_id_raw:
            try:
                lead = Lead.objects.get(pk=int(lead_id_raw))
            except (Lead.DoesNotExist, ValueError):
                lead = None

        # Expiry — default 30 days from now if blank
        expires_raw = (request.POST.get('expires_at') or '').strip()
        if expires_raw:
            try:
                expires_at = datetime.datetime.strptime(
                    expires_raw, '%Y-%m-%d').date()
            except ValueError:
                expires_at = (timezone.now()
                              + datetime.timedelta(days=30)).date()
        else:
            expires_at = (timezone.now()
                          + datetime.timedelta(days=30)).date()

        case_study_ids = request.POST.getlist('case_study_ids')

        proposal = Proposal.objects.create(
            lead=lead,
            prospect_name=(request.POST.get('prospect_name')
                           or '').strip()[:200],
            prospect_email=(request.POST.get('prospect_email')
                            or '').strip()[:254],
            prospect_business=(request.POST.get('prospect_business')
                               or '').strip()[:200],
            prospect_city=(request.POST.get('prospect_city')
                           or '').strip()[:100],
            prospect_state=(request.POST.get('prospect_state')
                            or '').strip()[:50],
            package=(request.POST.get('package') or '').strip()[:100],
            project_price=project_price,
            maintenance_price=maintenance_price,
            goals=(request.POST.get('goals') or '').strip(),
            pain_points=(request.POST.get('pain_points') or '').strip(),
            case_study_ids=list(case_study_ids),
            notes=(request.POST.get('notes') or '').strip(),
            expires_at=expires_at,
            status='draft',
        )

        # Auto-generate the PDF on save so the operator can preview
        # it immediately on the detail page.
        from clients.proposal_pdf import render_proposal_pdf
        try:
            proposal.pdf_path = render_proposal_pdf(proposal)
            proposal.save(update_fields=['pdf_path', 'updated_at'])
        except Exception:
            # Don't block proposal creation on PDF errors — show a
            # banner on the detail page instead.
            pass

        return redirect('admin_dashboard:proposal_detail',
                        proposal_id=proposal.id)

    leads = (Lead.objects
             .filter(status__in=['new', 'contacted', 'replied',
                                 'call_booked', 'proposal_sent'])
             .order_by('-created_at')[:200])
    case_studies = (CaseStudy.objects
                    .filter(is_published=True)
                    .select_related('client')
                    .order_by('-created_at'))

    build_tiers = ServiceTier.objects.filter(
        category='website_build', is_active=True).order_by('sort_order',
                                                           'price')
    maint_tiers = ServiceTier.objects.filter(
        category='maintenance', is_active=True).order_by('sort_order',
                                                        'price')

    return render(request, 'admin_dashboard/proposal_new.html',
                  _admin_context(
                      'proposals',
                      leads=leads,
                      case_studies=case_studies,
                      build_tiers=build_tiers,
                      maint_tiers=maint_tiers,
                  ))


@admin_required
def proposal_detail(request, proposal_id):
    """Single-proposal detail + action buttons."""
    from clients.models import CaseStudy, Proposal

    proposal = get_object_or_404(Proposal, id=proposal_id)

    case_studies = []
    if proposal.case_study_ids:
        case_studies = list(
            CaseStudy.objects.filter(id__in=proposal.case_study_ids))

    return render(request, 'admin_dashboard/proposal_detail.html',
                  _admin_context(
                      'proposals',
                      proposal=proposal,
                      case_studies=case_studies,
                  ))


@admin_required
@require_POST
def proposal_generate(request, proposal_id):
    """(Re)generate the proposal PDF on demand."""
    from clients.models import Proposal
    from clients.proposal_pdf import render_proposal_pdf

    proposal = get_object_or_404(Proposal, id=proposal_id)
    try:
        proposal.pdf_path = render_proposal_pdf(proposal)
        proposal.save(update_fields=['pdf_path', 'updated_at'])
    except Exception as exc:  # noqa: BLE001 — surface on detail page
        return HttpResponse(
            f'PDF generation failed: {exc}', status=500)
    return redirect('admin_dashboard:proposal_detail',
                    proposal_id=proposal.id)


@admin_required
@require_POST
def proposal_send(request, proposal_id):
    """Email the proposal PDF to the prospect via SendGrid."""
    import base64
    import os
    from pathlib import Path

    from clients.models import Proposal

    proposal = get_object_or_404(Proposal, id=proposal_id)
    if not proposal.prospect_email:
        return HttpResponseBadRequest('Prospect email is required.')
    if not proposal.pdf_path:
        return HttpResponseBadRequest(
            'Generate the PDF before sending.')

    abs_path = Path(settings.MEDIA_ROOT) / proposal.pdf_path
    if not abs_path.exists():
        return HttpResponseBadRequest(
            'PDF file is missing — regenerate first.')

    business = (proposal.prospect_business
                or proposal.prospect_name or 'your project')
    subject = f'Website Proposal — {business}'

    view_url = proposal.get_tracking_url()
    html_content = (
        f"<p>Hi {proposal.prospect_name.split()[0] if proposal.prospect_name else 'there'},</p>"
        f"<p>Attached is your proposal for "
        f"<strong>{business}</strong>. You can also view it online:</p>"
        f"<p><a href='{view_url}'>View proposal</a></p>"
        f"<p>It's good for 30 days. Reply to this email or "
        f"call/text 210-896-2536 with any questions.</p>"
        f"<p>— Zachery Long<br>"
        f"Aspired Websites LLC</p>"
    )

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Attachment, Disposition, FileContent, FileName,
            FileType, Mail,
        )
    except ImportError:
        return HttpResponse('SendGrid SDK not installed.', status=500)

    # SDK path — append the legal address footer manually since
    # AspiredEmailBackend doesn't see SendGrid SDK sends.
    from core.email_signature import append_signature
    _, html_content = append_signature(html=html_content)

    message = Mail(
        from_email=settings.DEFAULT_FROM_EMAIL,
        to_emails=proposal.prospect_email,
        subject=subject,
        html_content=html_content,
    )
    with open(abs_path, 'rb') as fh:
        encoded = base64.b64encode(fh.read()).decode()
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = ('application/pdf' if ext.lower() == '.pdf'
            else 'text/html')
    attachment = Attachment(
        FileContent(encoded),
        FileName(f'proposal{ext}'),
        FileType(mime),
        Disposition('attachment'),
    )
    message.attachment = attachment

    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        sg.send(message)
    except Exception as exc:  # noqa: BLE001 — surface to operator
        return HttpResponse(
            f'SendGrid error: {str(exc)[:200]}', status=500)

    proposal.sent_at = timezone.now()
    proposal.status = 'sent'
    if not proposal.expires_at:
        proposal.expires_at = (
            timezone.now() + datetime.timedelta(days=30)).date()
    proposal.save(update_fields=[
        'sent_at', 'status', 'expires_at', 'updated_at',
    ])

    return redirect('admin_dashboard:proposal_detail',
                    proposal_id=proposal.id)


@admin_required
@require_POST
def proposal_set_status(request, proposal_id):
    """Flip status to accepted/declined from the detail page buttons."""
    from clients.models import Proposal

    proposal = get_object_or_404(Proposal, id=proposal_id)
    new_status = (request.POST.get('status') or '').strip()
    valid = {choice for choice, _ in Proposal.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest('invalid status')
    proposal.status = new_status
    proposal.save(update_fields=['status', 'updated_at'])
    return redirect('admin_dashboard:proposal_detail',
                    proposal_id=proposal.id)


@admin_required
def proposal_lead_autofill(request):
    """HTMX endpoint — fill prospect fields when a Lead is picked."""
    from outreach.models import Lead

    lead_id = (request.GET.get('lead_id') or '').strip()
    if not lead_id:
        return HttpResponse('')
    try:
        lead = Lead.objects.get(pk=int(lead_id))
    except (Lead.DoesNotExist, ValueError):
        return HttpResponse('')

    return render(request, 'admin_dashboard/_proposal_autofill.html',
                  {'lead': lead})


