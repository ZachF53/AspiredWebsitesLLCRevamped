"""
Outreach management pages — offers, campaigns, and the review queue.

WHY NOT DJANGO ADMIN
--------------------
Django admin is fine for inspecting rows and terrible for running a
business. It shows every field with equal weight, so the one that
actually matters (does this campaign have an Instantly id?) sits in a
list beside twelve that do not, and it gives no room to explain WHY a
setting matters at the moment someone is changing it.

These pages exist so the whole outreach system can be operated without
ever opening /admin/.

SHAPE
-----
Each entity gets list / new / edit / delete, following the same pattern
as views_pricing.py. Deletes are POST-only and confirmed on their own
page; a GET must never destroy anything, because a prefetching browser
or a pasted link will eventually issue one.
"""

import logging

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from outreach import sequences
from outreach.models import Lead, Offer, OutreachCampaign

from .context import _admin_context
from .decorators import admin_required

logger = logging.getLogger(__name__)


@admin_required
def outreach_index(request):
    """Landing page for /admin-dashboard/outreach/.

    Existed as a URL prefix with nothing served at it, so the obvious
    place to navigate to 404'd. The counts double as the funnel: each
    number is a stage, and the gap between two of them is where leads
    are being lost.
    """
    from outreach import instantly, verify

    leads = Lead.objects.all()
    sendable = sum(
        1 for s in leads.values_list('email_verification_status', flat=True)
        if verify.is_sendable(s))
    campaigns = OutreachCampaign.objects.all()

    try:
        allowed, reason = instantly.sending_allowed()
    except Exception:            # noqa: BLE001 - the page must still render
        allowed, reason = False, 'Could not evaluate the send gates.'

    return render(request, 'admin_dashboard/outreach_index.html',
                  _admin_context(
                      active='outreach',
                      sending_allowed=allowed,
                      sending_reason=reason,
                      offer_count=Offer.objects.count(),
                      campaign_count=campaigns.count(),
                      pushable_count=sum(1 for c in campaigns if c.is_pushable),
                      review_count=leads.filter(needs_review=True).count(),
                      lead_count=leads.count(),
                      sendable_count=sendable,
                      enriched_count=leads.filter(
                          enrichment_completed_at__isnull=False).count(),
                      icebreaker_count=leads.exclude(icebreaker='').count(),
                      pushed_count=leads.exclude(instantly_lead_id='').count(),
                  ))


# ── Offers ─────────────────────────────────────────────────────────────

@admin_required
def offer_list(request):
    """Every offer, with its measured reply rate.

    Reply rate is the whole reason offers are rows rather than
    constants, so it is the column the page is built around.
    """
    offers = Offer.objects.annotate(
        campaign_count=Count('campaigns')).order_by('-active', 'name')
    return render(request, 'admin_dashboard/outreach_offer_list.html',
                  _admin_context(
                      active='outreach',
                      offers=offers,
                      active_count=sum(1 for o in offers if o.active),
                  ))


def _offer_form_fields(request, offer):
    """Read an offer out of POST. Returns (offer, errors)."""
    errors = []
    offer.name = (request.POST.get('name') or '').strip()
    offer.appeals_to = (request.POST.get('appeals_to') or '').strip()
    offer.fulfilment_cost = (request.POST.get('fulfilment_cost') or '').strip()
    offer.pitch = (request.POST.get('pitch') or '').strip()
    offer.restate = (request.POST.get('restate') or '').strip()
    offer.ask = (request.POST.get('ask') or '').strip()
    offer.active = 'active' in request.POST

    key = (request.POST.get('key') or '').strip() or slugify(offer.name)
    offer.key = slugify(key).replace('-', '_')[:60]

    if not offer.name:
        errors.append('Name is required.')
    if not offer.key:
        errors.append('Key is required (or give it a name to derive one).')
    if not offer.pitch:
        errors.append('Pitch is required - it is the offer itself.')
    if not offer.ask:
        errors.append('Ask is required - without it there is no way to say yes.')

    clash = Offer.objects.filter(key=offer.key)
    if offer.pk:
        clash = clash.exclude(pk=offer.pk)
    if clash.exists():
        errors.append(f'An offer with key "{offer.key}" already exists.')

    return offer, errors


def _preview_offer(offer):
    """Render touch 1 with this offer so the editor sees the real email.

    An offer is three fragments that only make sense inside the
    template. Editing them blind is how you end up with a sentence that
    reads correctly in the form and wrongly in the inbox.
    """
    try:
        steps = sequences.build_steps(
            'texas-law', offer=offer if offer.pk else None)
        return steps[0]['body'], sequences.describe_problems(steps)
    except sequences.SequenceError as exc:
        return '', [str(exc)]


@admin_required
def offer_edit(request, offer_id=None):
    """Create or edit one offer, with a live preview of the real email."""
    offer = (get_object_or_404(Offer, pk=offer_id) if offer_id
             else Offer(proposed_by='human'))
    errors = []

    if request.method == 'POST':
        offer, errors = _offer_form_fields(request, offer)
        if not errors:
            offer.save()
            messages.success(
                request,
                f'Offer "{offer.name}" saved.'
                + ('' if offer.active else ' It is inactive - activate it '
                                           'when you want it used.'))
            return redirect(reverse('admin_dashboard:outreach_offer_list'))

    preview, preview_problems = ('', [])
    if offer.pk and not errors:
        preview, preview_problems = _preview_offer(offer)

    return render(request, 'admin_dashboard/outreach_offer_edit.html',
                  _admin_context(
                      active='outreach',
                      offer=offer,
                      is_new=offer_id is None,
                      errors=errors,
                      preview=preview,
                      preview_problems=preview_problems,
                  ))


@admin_required
def offer_delete(request, offer_id):
    """Confirm on GET, delete on POST.

    Refuses while any campaign still points at it. The FK is PROTECT, so
    the database would refuse anyway; catching it here turns a 500 into
    a sentence explaining which campaigns to change first.
    """
    offer = get_object_or_404(Offer, pk=offer_id)
    campaigns = list(offer.campaigns.all())

    if request.method == 'POST':
        if campaigns:
            messages.error(
                request,
                f'"{offer.name}" is still used by {len(campaigns)} '
                f'campaign(s). Point them at another offer first.')
            return redirect(reverse('admin_dashboard:outreach_offer_list'))
        name = offer.name
        offer.delete()
        messages.success(request, f'Offer "{name}" deleted.')
        return redirect(reverse('admin_dashboard:outreach_offer_list'))

    return render(request, 'admin_dashboard/outreach_confirm_delete.html',
                  _admin_context(
                      active='outreach',
                      object_label=f'offer "{offer.name}"',
                      warning=(
                          f'Used by {len(campaigns)} campaign(s). Point '
                          f'them elsewhere first.' if campaigns else ''),
                      blockers=[c.name for c in campaigns],
                      cancel_url=reverse(
                          'admin_dashboard:outreach_offer_list'),
                  ))


@admin_required
@require_POST
def offer_toggle(request, offer_id):
    offer = get_object_or_404(Offer, pk=offer_id)
    offer.active = not offer.active
    offer.save(update_fields=['active', 'updated_at'])
    messages.success(
        request,
        f'"{offer.name}" is now {"active" if offer.active else "inactive"}.')
    return redirect(reverse('admin_dashboard:outreach_offer_list'))


# ── Campaigns ──────────────────────────────────────────────────────────

@admin_required
def campaign_list(request):
    """Campaigns, and specifically whether each one can actually push.

    A campaign needs BOTH an Instantly id and active=True. Showing only
    one of those is how a campaign sits looking enabled for a week while
    pushing nothing.
    """
    campaigns = OutreachCampaign.objects.select_related('offer').annotate(
        lead_count=Count('leads'),
        pushed_count=Count('leads', filter=Q(leads__instantly_lead_id__gt='')),
    ).order_by('-active', 'name')

    from outreach import instantly
    try:
        allowed, reason = instantly.sending_allowed()
    except Exception:            # noqa: BLE001 - page must still render
        allowed, reason = False, 'Could not evaluate the send gates.'

    return render(request, 'admin_dashboard/outreach_campaign_list.html',
                  _admin_context(
                      active='outreach',
                      campaigns=campaigns,
                      sending_allowed=allowed,
                      sending_reason=reason,
                  ))


@admin_required
def campaign_edit(request, campaign_id=None):
    campaign = (get_object_or_404(OutreachCampaign, pk=campaign_id)
                if campaign_id else OutreachCampaign())
    errors = []

    if request.method == 'POST':
        campaign.name = (request.POST.get('name') or '').strip()
        campaign.niche = (request.POST.get('niche') or '').strip()
        campaign.business_type = (
            request.POST.get('business_type') or '').strip()
        campaign.city = (request.POST.get('city') or '').strip()
        campaign.state = (request.POST.get('state') or '').strip().upper()[:2]
        campaign.instantly_campaign_id = (
            request.POST.get('instantly_campaign_id') or '').strip()
        campaign.active = 'active' in request.POST

        offer_id = request.POST.get('offer') or ''
        campaign.offer = (Offer.objects.filter(pk=offer_id).first()
                          if offer_id else None)

        raw_target = (request.POST.get('lead_target') or '').strip()
        if raw_target:
            try:
                campaign.lead_target = max(0, int(raw_target))
            except ValueError:
                errors.append('Lead target must be a whole number (0 = '
                              'unlimited).')
        else:
            campaign.lead_target = 0

        slug = (request.POST.get('slug') or '').strip() or campaign.name
        campaign.slug = slugify(slug)[:140]

        if not campaign.name:
            errors.append('Name is required.')
        if not campaign.slug:
            errors.append('Slug is required.')
        if not campaign.niche:
            errors.append('Niche is required.')
        clash = OutreachCampaign.objects.filter(slug=campaign.slug)
        if campaign.pk:
            clash = clash.exclude(pk=campaign.pk)
        if clash.exists():
            errors.append(f'A campaign with slug "{campaign.slug}" exists.')
        if campaign.active and not campaign.instantly_campaign_id:
            errors.append(
                'A campaign cannot be active without an Instantly campaign '
                'id - there would be nowhere to push leads to.')

        if not errors:
            campaign.save()
            messages.success(request, f'Campaign "{campaign.name}" saved.')
            return redirect(
                reverse('admin_dashboard:outreach_campaign_list'))

    return render(request, 'admin_dashboard/outreach_campaign_edit.html',
                  _admin_context(
                      active='outreach',
                      campaign=campaign,
                      is_new=campaign_id is None,
                      offers=Offer.objects.order_by('-active', 'name'),
                      errors=errors,
                  ))


@admin_required
def campaign_delete(request, campaign_id):
    campaign = get_object_or_404(OutreachCampaign, pk=campaign_id)
    lead_count = campaign.leads.count()

    if request.method == 'POST':
        name = campaign.name
        # Leads survive; they simply stop belonging to a campaign. Losing
        # a campaign row must never lose the leads that cost money.
        campaign.leads.update(campaign=None)
        campaign.delete()
        messages.success(
            request,
            f'Campaign "{name}" deleted. {lead_count} lead(s) kept and '
            f'unassigned.')
        return redirect(reverse('admin_dashboard:outreach_campaign_list'))

    return render(request, 'admin_dashboard/outreach_confirm_delete.html',
                  _admin_context(
                      active='outreach',
                      object_label=f'campaign "{campaign.name}"',
                      warning=(
                          f'{lead_count} lead(s) are assigned to it. They '
                          f'will be kept and unassigned, not deleted.'
                          if lead_count else ''),
                      blockers=[],
                      cancel_url=reverse(
                          'admin_dashboard:outreach_campaign_list'),
                  ))


# ── Review queue ───────────────────────────────────────────────────────

@admin_required
def review_queue(request):
    """Leads whose company NAME contradicts the industry the source gave.

    Held, never dropped: the signal is a heuristic on a name and names
    are ambiguous, so auto-discarding would lose real prospects with
    nobody finding out.
    """
    leads = Lead.objects.filter(needs_review=True).order_by('-created_at')
    return render(request, 'admin_dashboard/outreach_review_queue.html',
                  _admin_context(
                      active='outreach',
                      leads=leads,
                      total=leads.count(),
                  ))


@admin_required
@require_POST
def review_decide(request, lead_id):
    """Approve (release to campaigns) or reject (archive) one lead."""
    lead = get_object_or_404(Lead, pk=lead_id)
    decision = request.POST.get('decision')

    lead.needs_review = False
    lead.reviewed_at = timezone.now()
    lead.reviewed_by = request.user
    fields = ['needs_review', 'reviewed_at', 'reviewed_by', 'updated_at']

    if decision == 'reject':
        lead.status = 'archived'
        fields.append('status')
        messages.success(
            request, f'{lead.firm_name} archived - it stays out of every '
                     f'campaign.')
    else:
        messages.success(
            request, f'{lead.firm_name} approved and released.')

    lead.save(update_fields=fields)
    return redirect(reverse('admin_dashboard:outreach_review_queue'))


@admin_required
@require_POST
def review_bulk(request):
    """Approve or reject everything currently flagged, in one action."""
    decision = request.POST.get('decision')
    qs = Lead.objects.filter(needs_review=True)
    count = qs.count()

    updates = {
        'needs_review': False,
        'reviewed_at': timezone.now(),
        'reviewed_by': request.user,
    }
    if decision == 'reject':
        updates['status'] = 'archived'
    qs.update(**updates)

    messages.success(
        request,
        f'{count} lead(s) '
        f'{"archived" if decision == "reject" else "approved"}.')
    return redirect(reverse('admin_dashboard:outreach_review_queue'))
