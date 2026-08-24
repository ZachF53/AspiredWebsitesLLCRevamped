"""
Putting a lead into a campaign — the stage that did not exist.

THE BUG THIS MODULE CLOSES
--------------------------
``push_to_instantly_task`` selected what to send with::

    Lead.objects.filter(campaign=campaign, instantly_lead_id='', ...)

and the only line in the codebase that ever set ``lead.campaign`` was in
``instantly.push_leads``, AFTER a successful push, recording where the
lead had gone. So a lead had to already be in the campaign to be pushed,
and only entered the campaign by being pushed. The filter matched zero
rows, permanently.

What makes it worth a module rather than a one-line patch is HOW it
failed. Nothing raised. The pipeline sourced, verified, enriched and
wrote icebreakers correctly, then reported ``nothing ready`` on the hour,
every hour, looking completely healthy. That is the same shape as the 416
sends that produced no replies: every screen showed a plausible number
because nothing measured the step that was broken. So this stage reports
what it skipped and why, and ``outreach_status`` prints leads that are
ready but unassigned as a named blocker rather than a silent zero.

WHY THE ARM IS THE CAMPAIGN, NOT THE CITY
-----------------------------------------
A campaign is one Instantly campaign, and Instantly reports analytics per
campaign. That makes the campaign the unit of measurement, so it must be
the variable being tested — the offer.

Make the campaign a city instead and every offer blends inside it: the
reply rate for "Houston" cannot be decomposed into which of the six
offers earned the replies, and the A/B/C/D/E/F test becomes unreadable at
exactly the moment it starts producing data.

City survives that trade because we can measure it ourselves. ``Lead.city``
is on every row and replies come back through ``EmailReply.lead``, so
per-city reply rate is a Django query. Per-offer reply rate is not — it
only exists if the offers are in separate campaigns.

The rule: the campaign is the dimension we can ONLY see in Instantly.
Everything we can see in our own database stays a column.

Practically, for Texas law firms that means arms like ``TX Law — Security
Review`` and ``TX Law — Speed Audit``, with every city flowing through all
of them. Six arms is six Instantly campaigns to hand-build; six arms per
city would be several hundred.

BALANCE, NOT RANDOMNESS
-----------------------
Arms are filled by choosing the emptiest eligible one, not by random
assignment. Over thousands of leads the two converge, but the run that
matters is the first few hundred — where random assignment can leave one
arm 40% larger than another and make an offer look better than it is.
Deterministic balancing also makes a dry run reproducible, which is what
lets ``--dry-run`` below be trusted before real money is spent on sends.
"""

import logging

from django.db.models import Count

from outreach.instantly import segment_mismatch
from outreach.models import Lead, OutreachCampaign

logger = logging.getLogger(__name__)


def assignable_leads():
    """Leads that are ready to be placed in an arm but are not in one.

    Mirrors the readiness conditions ``push_to_instantly_task`` applies,
    minus the campaign itself. Inbound leads are excluded here as well as
    at push time — somebody who contacted us must never receive cold
    outreach, and that rule is cheaper to enforce before assignment than
    to catch after it.
    """
    return (Lead.objects
            .filter(campaign__isnull=True,
                    instantly_lead_id='',
                    unsubscribed=False,
                    needs_review=False)
            .exclude(email='')
            .exclude(icebreaker='')
            .exclude(source__in=Lead.INBOUND_SOURCES)
            .order_by('-score', '-created_at'))


def open_campaigns():
    """Arms currently accepting leads, emptiest first.

    ``leads.count()`` is annotated once for the whole set rather than
    re-queried per lead: assigning a 700-lead batch across six arms would
    otherwise issue several thousand COUNT queries.
    """
    campaigns = (OutreachCampaign.objects
                 .filter(active=True)
                 .exclude(instantly_campaign_id='')
                 .annotate(assigned=Count('leads')))
    return [c for c in campaigns
            if not c.lead_target or c.assigned < c.lead_target]


def eligible_campaigns(lead, campaigns=None):
    """Every open arm this lead legitimately belongs in.

    Eligibility is ``segment_mismatch`` returning '' — the same function
    the push gate uses. Reusing it rather than reimplementing the check
    is deliberate: two copies of "does this lead match this segment?"
    would eventually disagree, and the failure mode of disagreement is a
    Los Angeles staffing firm receiving an email about Texas law firms,
    which is the exact pairing that gate was written to stop.
    """
    if campaigns is None:
        campaigns = open_campaigns()
    return [c for c in campaigns if not segment_mismatch(lead, c)]


def assign_leads(limit=500, dry_run=False):
    """Place ready leads into the emptiest arm each one qualifies for.

    Returns a summary dict. ``dry_run`` computes and reports the identical
    plan without writing, so the split can be inspected before any lead
    becomes a real send.
    """
    summary = {
        'assigned': 0,
        'skipped_no_campaign': 0,
        'by_campaign': {},
        'reasons': {},
        'dry_run': dry_run,
    }

    campaigns = open_campaigns()
    if not campaigns:
        summary['reasons']['no open campaigns'] = (
            'Every campaign is inactive, missing an Instantly id, or has '
            'reached its lead_target.')
        return summary

    # Local tallies so the emptiest-arm choice reflects leads assigned
    # earlier in THIS batch. Without it a 600-lead run would read the same
    # starting counts 600 times and drop the entire batch into whichever
    # arm happened to be smallest when the run began.
    counts = {c.pk: c.assigned for c in campaigns}

    for lead in assignable_leads()[:limit]:
        options = eligible_campaigns(lead, campaigns)
        options = [c for c in options
                   if not c.lead_target or counts[c.pk] < c.lead_target]

        if not options:
            summary['skipped_no_campaign'] += 1
            reason = _no_campaign_reason(lead, campaigns)
            summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1
            continue

        chosen = min(options, key=lambda c: (counts[c.pk], c.pk))

        if not dry_run:
            lead.campaign = chosen
            lead.save(update_fields=['campaign', 'updated_at'])

        counts[chosen.pk] += 1
        summary['assigned'] += 1
        summary['by_campaign'][chosen.name] = (
            summary['by_campaign'].get(chosen.name, 0) + 1)

    logger.info('assign_leads: %s', summary)
    return summary


def _no_campaign_reason(lead, campaigns):
    """Why no open arm would take this lead.

    Reported as an aggregate rather than per lead. "312 leads are in a
    state no campaign targets" is actionable; 312 identical log lines are
    not.
    """
    if not campaigns:
        return 'no open campaigns'
    reasons = {segment_mismatch(lead, c) for c in campaigns}
    reasons.discard('')
    if not reasons:
        return 'all matching arms are full'
    # Every arm rejected it, so the shared cause is worth naming.
    if all('campaign targets' in r and 'Lead is in' in r for r in reasons):
        return f'no campaign targets state {lead.state or "(unknown)"}'
    if all('campaign targets' in r and 'Lead is a' in r for r in reasons):
        return (f'no campaign targets business type '
                f'{lead.business_type or "(none)"}')
    return 'no campaign matches this segment'
