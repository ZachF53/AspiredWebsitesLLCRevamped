"""
The tools Prospect can call — §5.2.

WHAT AN AGENT IS, HERE
----------------------
Everything below this line already ran on a Celery schedule. Sourcing,
verification, enrichment, icebreakers, assignment and push are ordinary
functions that a beat entry calls in a fixed order, and they will keep
working if this module is deleted.

So the runtime is not what makes the pipeline go. It is what makes the
*decisions the fixed order cannot make*: whether Houston is exhausted
enough to move to Dallas, whether an arm has collected a readable sample,
whether today's unassigned pile means a missing campaign or a bad scrape.
Those are judgement calls on live numbers, and the alternative to an
agent making them is Zach making them, by hand, every day.

That framing decides the tool boundary. A tool exists when a decision
precedes it. There is no ``send_email`` tool because nothing decides per
email — Instantly sends on its own schedule once leads are pushed.

THE THREE CLASSES OF TOOL
-------------------------
  READ      free, no side effects, always allowed.
  ACT       changes our own database. Reversible. Runs immediately.
  COMMIT    spends money or causes a stranger to be emailed. NEVER runs
            when the model asks. Files for approval and stops.

The COMMIT class is the load-bearing one. Prospect can decide to scrape
Houston, but the charge only lands when a human clicks approve, and the
approved call is executed on the NEXT run rather than inside the click
(see ``execute_approved`` below). Two people have to agree — one of whom
is a person — before money moves or mail goes out.

WHEN AN APPROVED CALL ACTUALLY RUNS
-----------------------------------
Two paths, and they differ on purpose.

``ai_action_decide`` — the detail page — records the answer and returns.
The approved row is picked up at the start of the NEXT run by
``execute_approved``. That is right for a nightly agent: nobody is
watching, and there is no reason to run a scrape inside a click.

``ai_chat_decide`` — the Approve button in a conversation — queues the
work immediately and the thread polls for it. Parking it would have made
the chat useless: you would approve a scrape and nothing would happen
until morning, in a window you are sitting in front of waiting.

Neither runs the tool inside the request. A paid Apify scrape in a
request/response cycle would block the worker, and a timeout would leave
"did it charge me?" unanswerable.

Both stamp ``executed_at``, and both refuse to act on a row that already
carries one. Approval is permanent, so without that guard a retried task
or a double-click charges the card twice.
"""

import json
import logging

from django.db.models import Q
from django.utils import timezone

from outreach import spend

logger = logging.getLogger(__name__)

READ, ACT, COMMIT = 'read', 'act', 'commit'


def _noop_report(_line):
    """Default progress reporter — drops the line.

    Defined up here because every tool implementation takes it as a
    DEFAULT ARGUMENT, and defaults are evaluated when the `def` runs.
    Declaring it lower down beside the real reporter raised NameError at
    import time.

    Tools call report() unconditionally; when nobody is listening the
    line goes nowhere rather than being guarded at every call site.
    """


# ── Tool definitions ──────────────────────────────────────────────────
#
# Descriptions are written FOR THE MODEL and carry the operating rules
# with them. A rule that lives only in the system prompt competes with
# everything else in the prompt; a rule attached to the tool is read at
# the moment the tool is considered.

TOOLS = [
    {
        'name': 'funnel_status',
        'kind': READ,
        'description': (
            'The whole outreach funnel as numbers: how many leads exist, '
            'how many are verified sendable, enriched, personalised, '
            'assigned to an arm, and pushed. Also which campaigns are '
            'open and how full each one is. Call this FIRST on every run '
            '— every other decision depends on it.'),
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'city_progress',
        'kind': READ,
        'description': (
            'Per-city lead counts for a state: how many were sourced, how '
            'many became sendable, and how many are still unprocessed. '
            'Use this to decide whether the current city is exhausted and '
            'it is time to source the next one. A city is exhausted when '
            'almost nothing is left unprocessed — NOT when it merely has '
            'fewer leads than you expected, which usually means the '
            'scrape under-delivered rather than the city being finished.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'state': {
                    'type': 'string',
                    'description': 'Two-letter state code, e.g. TX.'},
            },
            'required': ['state'],
        },
    },
    {
        'name': 'run_pipeline',
        'kind': ACT,
        'description': (
            'Run verify -> enrich -> verify -> icebreaker -> assign for '
            'leads that still need it. This is the normal way to move '
            'leads forward. It costs a small amount of Claude and '
            'PageSpeed usage per lead and is safe to call once per run. '
            'It does NOT push anything to Instantly and cannot cause an '
            'email to be sent.'),
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'assign_campaigns',
        'kind': ACT,
        'description': (
            'Place ready leads into campaign arms, filling the emptiest '
            'eligible arm first. Free and reversible. Use dry_run=true to '
            'see the split before committing to it. Leads that match no '
            'open arm are reported with the reason — that report is how '
            'you discover a missing campaign.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'dry_run': {
                    'type': 'boolean',
                    'description': 'Preview without writing. Default false.'},
            },
        },
    },
    {
        'name': 'start_scrape',
        'kind': COMMIT,
        'description': (
            'Source new leads for one city from Apify. THIS COSTS MONEY '
            'and requires human approval before it runs — calling it '
            'files a request and returns immediately, having done '
            'nothing. Zachery sees an Approve button in the chat; if he '
            'clicks it the scrape runs straight away and you are told '
            'the result. Say plainly that it is filed and has NOT run. '
            'Only request this when the current city is genuinely '
            'exhausted, and never while one is already awaiting '
            'approval — check funnel_status first.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'city': {'type': 'string'},
                'state': {
                    'type': 'string',
                    'description': 'Two-letter state code, e.g. TX.'},
                'niche': {
                    'type': 'string',
                    'description': 'What to search for, e.g. "law firm". '
                                   'Defaults to law firm.'},
                'limit': {
                    'type': 'integer',
                    'description': 'Max leads to pull. Apify free plan '
                                   'caps this at 100.'},
                'reason': {
                    'type': 'string',
                    'description': 'Why this city, now. The human reads '
                                   'this when deciding — state the '
                                   'evidence, not the intention.'},
            },
            'required': ['city', 'state', 'reason'],
        },
    },
    {
        'name': 'import_dataset',
        'kind': ACT,
        'description': (
            'Import leads from an Apify dataset that a HUMAN already ran '
            'in the Apify Console. Free — dataset reads are not billed, '
            'and the money was already spent deliberately by a person, '
            'so this needs no approval. This is the working path while '
            'the Apify plan is free: start_scrape is refused by the '
            'actor on that plan. Get the dataset id from your assigned '
            'tasks or from the human.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'dataset_id': {'type': 'string'},
                'label': {'type': 'string'},
            },
            'required': ['dataset_id'],
        },
    },
    {
        'name': 'campaign_stats',
        'kind': READ,
        'description': (
            'Every campaign arm with its LIVE Instantly numbers — leads, '
            'emails sent, opens, replies, bounces — merged with what '
            'Django knows about it (offer, assigned, pushed, whether it '
            'can push at all). Use this whenever asked how campaigns are '
            'doing, which arm is winning, or what exists. It reports '
            'whether each arm has enough volume for its reply rate to '
            'mean anything; below that, say the rate is noise rather '
            'than ranking arms by it.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'campaign': {
                    'type': 'string',
                    'description': 'Optional name or slug to limit to '
                                   'one arm.'},
            },
        },
    },
    {
        'name': 'preview_sequence',
        'kind': READ,
        'description': (
            'Show the actual email copy a sequence would send — every '
            'touch, its subject, its body and how many days after the '
            'previous one it goes. Free and writes nothing. Give an '
            'offer key to see how that offer reads, or a campaign to see '
            'what that arm is currently set to send. ALWAYS use this '
            'before set_campaign_sequence, and show Zachery the copy: he '
            'should read what strangers will receive before approving '
            'it, not after.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'offer': {
                    'type': 'string',
                    'description': 'Offer key, e.g. security_review. '
                                   'list_offers shows them.'},
                'sequence': {
                    'type': 'string',
                    'description': 'Sequence template slug. Defaults to '
                                   'texas-law.'},
                'campaign': {
                    'type': 'string',
                    'description': 'Campaign name or slug — preview what '
                                   'this arm is set up to send.'},
            },
        },
    },
    {
        'name': 'set_campaign_sequence',
        'kind': COMMIT,
        'description': (
            'Write the email sequence into an EXISTING campaign, '
            'replacing whatever it currently sends. Requires human '
            'approval. Two ways to use it: give an `offer` key to '
            'compose the approved template around a different offer '
            '(preferred — that copy has already been through '
            'pre-flight), or give `steps` to write the copy yourself. '
            'Either way the copy is pre-flighted before anything is '
            'written, and Zachery sees the full text on the approval '
            'card. Preview it with preview_sequence and show him first. '
            'This never starts a campaign — a paused arm stays paused.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'campaign': {
                    'type': 'string',
                    'description': 'Campaign name or slug to write into.'},
                'offer': {
                    'type': 'string',
                    'description': 'Compose the standard template around '
                                   'this offer key.'},
                'sequence': {
                    'type': 'string',
                    'description': 'Template slug. Defaults to texas-law.'},
                'steps': {
                    'type': 'array',
                    'description': 'Custom copy, one object per touch. '
                                   'Use only when asked for bespoke '
                                   'wording — the template is safer.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'subject': {
                                'type': 'string',
                                'description': 'Blank on follow-ups so '
                                               'they thread under touch '
                                               'one.'},
                            'body': {'type': 'string'},
                            'delay_days': {
                                'type': 'integer',
                                'description': 'Days to wait after the '
                                               'previous touch.'},
                        },
                        'required': ['body'],
                    },
                },
                'reason': {
                    'type': 'string',
                    'description': 'What is changing and why. Zachery '
                                   'reads this when deciding.'},
            },
            'required': ['campaign', 'reason'],
        },
    },
    {
        'name': 'create_campaign',
        'kind': COMMIT,
        'description': (
            'Create a NEW campaign in Instantly, PAUSED, and register the '
            'matching arm in Django so leads can later be assigned to it. '
            'Requires human approval. Creates nothing that can send on '
            'its own: the campaign is paused on arrival and only a human '
            'can start it in Instantly\'s own UI. Use this when leads are '
            'ready but there is no arm for their niche and state — '
            'list_campaigns tells you whether one already exists. Do NOT '
            'use it to fix a campaign that exists but is missing an '
            'Instantly id; say so instead.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'name': {
                    'type': 'string',
                    'description': 'Human label, e.g. '
                                   '"TX Law [security review]".'},
                'niche': {'type': 'string',
                          'description': 'e.g. "family law".'},
                'state': {'type': 'string',
                          'description': 'Two-letter code, e.g. TX.'},
                'offer': {
                    'type': 'string',
                    'description': 'Offer key the copy carries, e.g. '
                                   'security_review. list_offers shows '
                                   'them.'},
                'sequence': {
                    'type': 'string',
                    'description': 'Sequence template slug. Defaults to '
                                   'texas-law, the only one written.'},
                'reason': {
                    'type': 'string',
                    'description': 'Why a new arm is needed rather than '
                                   'an existing one.'},
            },
            'required': ['name', 'niche', 'state', 'reason'],
        },
    },
    {
        'name': 'push_to_instantly',
        'kind': COMMIT,
        'description': (
            'Push assigned leads into their Instantly campaigns, which '
            'queues REAL EMAILS TO REAL STRANGERS. Requires human '
            'approval; calling it files a request and does nothing. Also '
            'gated independently by the warmup check and the sending '
            'switch — approval alone will not send if mailboxes are not '
            'warm. Only ever pushes into a campaign that already exists '
            'and is active: if the leads have no arm, use create_campaign '
            'first and wait for that to be approved and started. Say in '
            '`reason` how many leads and which arms.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'reason': {
                    'type': 'string',
                    'description': 'How many leads, which arms, and why '
                                   'now.'},
            },
            'required': ['reason'],
        },
    },
    {
        'name': 'write_journal',
        'kind': ACT,
        'description': (
            'Record what you did and what the next run should know. This '
            'is your ONLY memory between runs — you will not otherwise '
            'remember this session. Write the state of play, not a list '
            'of actions: which city is in progress, what you are waiting '
            'on, what you would do next and why. Call this LAST, once, '
            'on every run.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'entry': {'type': 'string'},
            },
            'required': ['entry'],
        },
    },

    # ── Lookup tools ──────────────────────────────────────────────────
    #
    # Added for the chat pane. The original set answered "what should I
    # do next" — aggregate counts and actions — and could not answer
    # "show me the icebreaker for the people we have now", which is the
    # first thing a human actually asks. All READ: they only look.

    {
        'name': 'find_leads',
        'kind': READ,
        'description': (
            'Search leads and get them back one row per lead — firm, '
            'contact name, email, city, status, score, and whether an '
            'icebreaker has been written. Use this whenever you are asked '
            'about specific leads, "who have we got", or anything that '
            'needs names rather than totals. Every filter is optional; '
            'with none you get the most recent leads.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'Match against firm name, contact name '
                                   'or email.'},
                'city': {'type': 'string'},
                'state': {'type': 'string',
                          'description': 'TX or GA.'},
                'status': {'type': 'string',
                           'description': 'Lead status, e.g. new.'},
                'has_icebreaker': {
                    'type': 'boolean',
                    'description': 'True for only leads with a written '
                                   'icebreaker, False for only those '
                                   'without.'},
                'limit': {'type': 'integer',
                          'description': 'Default 25, maximum 100.'},
            },
        },
    },
    {
        'name': 'lead_detail',
        'kind': READ,
        'description': (
            'Everything known about ONE lead, including the full '
            'icebreaker text, the measured site signals it was written '
            'from (PageSpeed, TLS, copyright year), verification status, '
            'campaign assignment and notes. Identify the lead by id, '
            'email, or firm name. Use this when asked to show or judge '
            'the actual copy for someone.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'lead': {
                    'type': 'string',
                    'description': 'Lead id, email address, or firm name.'},
            },
            'required': ['lead'],
        },
    },
    {
        'name': 'list_campaigns',
        'kind': READ,
        'description': (
            'Every campaign arm: its niche and state, which offer it '
            'carries, whether it is active and pushable, how many leads '
            'are assigned, and how many have been pushed. Use this when '
            'asked why leads are not going anywhere, or which arm is '
            'which.'),
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'list_offers',
        'kind': READ,
        'description': (
            'The offers a campaign can carry — name, what it appeals to, '
            'the honest fulfilment cost, the pitch text, and its sends / '
            'replies / bookings counters. Use this when asked what an '
            'offer actually says or which is performing.'),
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'recent_replies',
        'kind': READ,
        'description': (
            'Inbound replies with how each was classified and whether it '
            'still needs a human. Use this when asked whether anyone has '
            'replied, or what they said.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer',
                          'description': 'Default 15, maximum 50.'},
            },
        },
    },
    {
        'name': 'sourcing_history',
        'kind': READ,
        'description': (
            'Recent Apify sourcing runs: what was asked for, how many '
            'rows came back, how many the ICP screen rejected and why, '
            'and what each run cost. Use this when asked where leads came '
            'from, why so few survived, or what sourcing has cost.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer',
                          'description': 'Default 10, maximum 30.'},
            },
        },
    },
    {
        'name': 'spend_summary',
        'kind': READ,
        'description': (
            'Money: your own Claude spend today against the daily cap, '
            'and Apify spend this month against its budget. Use this when '
            'asked what anything has cost or how much room is left.'),
        'input_schema': {'type': 'object', 'properties': {}},
    },
]

_BY_NAME = {t['name']: t for t in TOOLS}


def anthropic_tools(exclude=()):
    """TOOLS minus our own bookkeeping, in the shape the API expects.

    ``exclude`` withholds tools by name. Withholding is a real guard —
    the model cannot call a tool it was never offered — whereas telling
    it not to in the system prompt is a request competing with every
    other line in the prompt. The chat pane uses this to drop
    write_journal, which would otherwise let a passing question overwrite
    the memory a scheduled run wrote.
    """
    drop = set(exclude or ())
    return [{k: v for k, v in t.items() if k != 'kind'}
            for t in TOOLS if t['name'] not in drop]


def tool_kind(name):
    tool = _BY_NAME.get(name)
    return tool['kind'] if tool else None


# ── Implementations ───────────────────────────────────────────────────

def _funnel_status(_input, report=_noop_report):
    from outreach.assignment import assignable_leads, open_campaigns
    from outreach.models import Lead, OutreachCampaign
    from outreach import instantly, verify

    leads = Lead.objects.all()
    sendable = sum(
        1 for s in leads.values_list('email_verification_status', flat=True)
        if verify.is_sendable(s))

    arms = open_campaigns()
    allowed, why = instantly.sending_allowed()

    return {
        'leads_total': leads.count(),
        'sendable': sendable,
        'enriched': leads.filter(
            enrichment_completed_at__isnull=False).count(),
        'has_icebreaker': leads.exclude(icebreaker='').count(),
        'needs_review': leads.filter(needs_review=True).count(),
        'ready_but_unassigned': assignable_leads().count(),
        'assigned': leads.filter(campaign__isnull=False).count(),
        'pushed': leads.exclude(instantly_lead_id='').count(),
        'campaigns': [
            {
                'name': c.name,
                'state': c.state,
                'offer': c.offer.name if c.offer else '(default)',
                'leads': c.leads.count(),
                'target': c.lead_target or 'unlimited',
                'active': c.active,
                'has_instantly_id': bool(c.instantly_campaign_id),
                'accepting': c in arms,
            }
            for c in OutreachCampaign.objects.all()
        ],
        'sending_allowed': allowed,
        'sending_blocked_because': why,
        'spend_today_usd': float(spend.spent_today()),
        'spend_cap_usd': float(spend.daily_cap()),
    }


def _city_progress(tool_input, report=_noop_report):
    from django.db.models import Count, Q
    from outreach.models import Lead

    state = (tool_input.get('state') or '').strip()
    if not state:
        return {'error': 'state is required.'}

    # Both spellings, because sources disagree: Apify writes 'TX' and
    # Google Places writes 'Texas'. Matching only one silently reports a
    # city as empty when it is merely stored under the other.
    from outreach.instantly import _STATE_ABBREV
    long_names = [k for k, v in _STATE_ABBREV.items()
                  if v == state.upper()]
    q = Q(state__iexact=state)
    for name in long_names:
        q |= Q(state__iexact=name)

    rows = (Lead.objects.filter(q)
            .values('city')
            .annotate(
                total=Count('id'),
                personalised=Count('id', filter=~Q(icebreaker='')),
                assigned=Count('id', filter=Q(campaign__isnull=False)),
                pushed=Count('id', filter=~Q(instantly_lead_id='')))
            .order_by('-total'))

    cities = [
        {
            'city': r['city'] or '(unknown)',
            'sourced': r['total'],
            'personalised': r['personalised'],
            'assigned': r['assigned'],
            'pushed': r['pushed'],
            'unprocessed': r['total'] - r['personalised'],
        }
        for r in rows
    ]
    return {'state': state.upper(), 'cities': cities,
            'city_count': len(cities)}


def _run_pipeline(_input, report=_noop_report):
    from outreach.tasks import (
        assign_campaigns_task, enrich_pending_leads_task,
        generate_icebreakers_task, verify_leads_task,
    )
    allowed, why = spend.check_spend_allowed()
    if not allowed:
        report('refused — daily AI spend cap reached')
        return {'ran': False, 'reason': why}

    # Each stage reports as it finishes. This can run for minutes on a
    # fresh batch, and a chat that says nothing for four minutes is
    # indistinguishable from one that has died.
    report('verifying addresses…')
    verify_pre = verify_leads_task()
    report(f'verify: {verify_pre}')

    report('enriching (PageSpeed, TLS, socials)…')
    enrich = enrich_pending_leads_task()
    report(f'enrich: {enrich}')

    report('re-verifying anything enrichment changed…')
    verify_post = verify_leads_task()
    report(f'verify: {verify_post}')

    report('writing icebreakers…')
    icebreakers = generate_icebreakers_task()
    report(f'icebreakers: {icebreakers}')

    report('assigning to campaign arms…')
    assign = assign_campaigns_task()
    report(f'assign: {assign}')

    return {
        'ran': True,
        'verify_pre': verify_pre,
        'enrich': enrich,
        'verify_post': verify_post,
        'icebreakers': icebreakers,
        'assign': assign,
    }


def _assign_campaigns(tool_input, report=_noop_report):
    from outreach.assignment import assign_leads
    return assign_leads(dry_run=bool(tool_input.get('dry_run')))


def _start_scrape(tool_input, report=_noop_report):
    """Runs only after approval — see execute_approved.

    Two failure modes are reported as outcomes rather than errors,
    because both are states of the world the agent must reason about
    rather than bugs it should retry through:

    - quota reached: the daily run count or the monthly dollar budget.
    - actor refused: Apify's FREE plan blocks API-triggered runs for
      code_crafter/leads-finder. The actor still bills the start event
      and writes an error row, so this is not "found nothing" — it is a
      paid no-op. The escape is the UI + import_dataset path.
    """
    from outreach.apify_source import (
        ApifyActorRefused, ApifyError, ApifyQuotaReached, run_lead_search,
    )
    from outreach.pipeline import import_leads

    city = (tool_input.get('city') or '').strip()
    state = (tool_input.get('state') or '').strip()
    niche = (tool_input.get('niche') or 'law firm').strip()
    limit = int(tool_input.get('limit') or 100)

    if not city:
        return {'scraped': False, 'reason': 'city is required.'}

    # NOT niche.title(). "family law" is a search for a LAW FIRM; using
    # the niche as the type rejects every row and still bills the run.
    # See apify_source.business_type_for_niche.
    from outreach.apify_source import business_type_for_niche
    business_type = ((tool_input.get('business_type') or '').strip()
                     or business_type_for_niche(niche))

    report(f'searching Apify — {niche} in {city}, {state} (max {limit})')
    try:
        leads, ledger = run_lead_search(
            niche=niche, city=city, state=state, max_results=limit,
            label=f'{niche} in {city}, {state} (Prospect)',
            business_type=business_type)
    except ApifyQuotaReached as exc:
        return {'scraped': False, 'quota_reached': True, 'reason': str(exc)}
    except ApifyActorRefused as exc:
        return {
            'scraped': False,
            'actor_refused': True,
            'reason': str(exc),
            'what_to_do': (
                'Apify\'s free plan blocks API-triggered runs for this '
                'actor. No retry will fix it. Ask the human to run the '
                'search in the Apify Console and give you the dataset id, '
                'then call import_dataset. Note this in your journal so '
                'the next run does not try again.'),
        }
    except ApifyError as exc:
        return {'scraped': False, 'reason': str(exc)}

    report(f'{ledger.results_returned} returned by Apify')
    if ledger.results_rejected:
        report(f'{ledger.results_rejected} rejected by the ICP screen '
               f'(size, title, state or industry)')
    report(f'{len(leads)} usable — importing')

    # Stamp the segment type, not the search phrase, or the leads clear
    # the ICP screen and are then blocked at push for being "Family Law"
    # in a "Law Firm" campaign.
    result = import_leads(leads, source='apify',
                          business_type_override=business_type or None)
    report(f"imported {result.get('imported', 0)} new, "
           f"{result.get('duplicates', 0)} duplicates, "
           f"${float(ledger.actual_cost_usd or 0):.2f} spent")
    result['apify_run_id'] = ledger.apify_run_id
    result['cost_usd'] = float(ledger.actual_cost_usd or 0)
    result['returned_by_apify'] = ledger.results_returned
    result['rejected_by_icp_screen'] = ledger.results_rejected
    result['business_type'] = business_type or '(unconstrained)'
    result['scraped'] = True
    return result


def _import_dataset(tool_input, report=_noop_report):
    """Import an Apify dataset a human triggered in the Console.

    The free-plan path. Dataset reads are NOT billed, so unlike
    start_scrape this costs nothing and does not need approval — the
    money was already spent, by a human, deliberately.
    """
    from outreach.apify_source import ApifyError, import_from_dataset
    from outreach.pipeline import import_leads

    dataset_id = (tool_input.get('dataset_id') or '').strip()
    if not dataset_id:
        return {'imported': False, 'reason': 'dataset_id is required.'}
    try:
        # Returns raw mapped dicts, NOT saved rows -- the dedup, scoring
        # and suppression checks all live in import_leads.
        leads, ledger = import_from_dataset(
            dataset_id, label=tool_input.get('label') or 'Prospect import')
    except ApifyError as exc:
        return {'imported': False, 'reason': str(exc)}

    result = import_leads(leads, source='apify')
    result['dataset_id'] = dataset_id
    result['returned_by_apify'] = ledger.results_returned
    return result


# Below this many delivered emails, one arm's reply rate cannot be told
# apart from another's. The system prompt says the same thing in prose;
# reporting it per arm makes it a number the model cannot round past.
MIN_MEANINGFUL_SENDS = 300


def _first_of(d, *keys, default=0):
    """First present key. Instantly has renamed these fields before, and
    a stats tool that silently reports 0 for 'replies' because the key
    moved is worse than one that reports nothing."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _campaign_stats(tool_input, report=_noop_report):
    """Local arms merged with live Instantly counters."""
    from outreach import instantly
    from outreach.models import OutreachCampaign

    only = (tool_input.get('campaign') or '').strip()
    campaigns = OutreachCampaign.objects.select_related('offer').all()
    if only:
        campaign, err = _find_campaign(only)
        if err:
            return err
        campaigns = [campaign]

    report('reading Instantly analytics')
    analytics_by_id, analytics_error = {}, ''
    try:
        rows = instantly.campaign_analytics() or []
        for row in rows:
            key = _first_of(row, 'campaign_id', 'id', default=None)
            if key:
                analytics_by_id[str(key)] = row
    except Exception as exc:  # noqa: BLE001
        # Reported, not raised. The local half is still worth having, and
        # "Instantly is unreachable" is itself an answer.
        logger.exception('campaign_analytics failed')
        analytics_error = str(exc)[:300]

    out = []
    for c in campaigns:
        live = analytics_by_id.get(str(c.instantly_campaign_id or ''), {})
        sent = int(_first_of(live, 'emails_sent_count', 'sent_count',
                             'contacted_count'))
        replies = int(_first_of(live, 'reply_count', 'replies_count'))
        opens = int(_first_of(live, 'open_count', 'opens_count'))
        bounces = int(_first_of(live, 'bounced_count', 'bounce_count'))

        row = {
            'name': c.name,
            'slug': c.slug,
            'niche': c.niche,
            'state': c.state,
            'business_type': c.business_type,
            'offer': c.offer.name if c.offer_id else None,
            'active': c.active,
            'has_instantly_id': bool(c.instantly_campaign_id),
            'pushable': bool(c.active and c.instantly_campaign_id),
            'leads_assigned': c.leads.count(),
            'leads_pushed': c.leads_pushed,
            'emails_sent': sent,
            'opens': opens,
            'replies': replies,
            'bounces': bounces,
        }
        if sent:
            row['reply_rate_pct'] = round(replies * 100.0 / sent, 2)
            row['bounce_rate_pct'] = round(bounces * 100.0 / sent, 2)
            # Above 3% Google and Microsoft start filtering the domain,
            # and no amount of warming undoes it.
            row['bounce_rate_is_dangerous'] = (
                bounces * 100.0 / sent) > 3.0
        row['enough_data_to_judge'] = sent >= MIN_MEANINGFUL_SENDS
        if not row['enough_data_to_judge']:
            row['caveat'] = (
                f'{sent} sends is below {MIN_MEANINGFUL_SENDS}; any reply '
                f'rate here is noise and must not be used to rank arms.')
        out.append(row)

    result = {
        'campaigns': out,
        'count': len(out),
        'min_sends_before_a_rate_means_anything': MIN_MEANINGFUL_SENDS,
    }
    if analytics_error:
        result['instantly_unreachable'] = analytics_error
        result['note'] = ('Live send/open/reply numbers are missing — '
                          'those columns are zero because Instantly could '
                          'not be reached, NOT because nothing was sent.')
    return result


def _find_campaign(ref):
    """Resolve a campaign by slug or name. Returns (campaign, error)."""
    from outreach.models import OutreachCampaign

    ref = (ref or '').strip()
    if not ref:
        return None, 'Name the campaign.'
    campaign = OutreachCampaign.objects.filter(slug=ref).first()
    if campaign is None:
        matches = list(OutreachCampaign.objects.filter(name__icontains=ref)[:5])
        if len(matches) > 1:
            names = ', '.join(m.slug for m in matches)
            return None, (f'{ref!r} matches several campaigns ({names}). '
                          f'Use the slug.')
        campaign = matches[0] if matches else None
    if campaign is None:
        return None, (f'No campaign matches {ref!r}. Call list_campaigns.')
    return campaign, ''


def _steps_from_input(tool_input, campaign=None):
    """Custom steps if given, otherwise compose the template.

    Returns ``(steps, source, error)``.
    """
    from outreach import sequences

    raw_steps = tool_input.get('steps')
    if raw_steps:
        steps = []
        for i, step in enumerate(raw_steps, 1):
            if not isinstance(step, dict):
                return None, '', f'Step {i} is not an object.'
            steps.append({
                'subject': (step.get('subject') or '').strip(),
                'body': (step.get('body') or '').strip(),
                'delay_days': int(step.get('delay_days') or (0 if i == 1
                                                             else 3)),
            })
        return steps, 'custom copy', ''

    from outreach.models import Offer
    offer_key = (tool_input.get('offer') or '').strip()
    if not offer_key and campaign is not None and campaign.offer_id:
        offer_key = campaign.offer.key
    offer_key = offer_key or sequences.DEFAULT_OFFER

    offer = Offer.objects.filter(key=offer_key).first()
    if offer is None:
        return None, '', f'No offer with key {offer_key!r}. Call list_offers.'

    slug = (tool_input.get('sequence') or 'texas-law').strip()
    try:
        steps = sequences.build_steps(slug, offer=offer)
    except sequences.SequenceError as exc:
        return None, '', str(exc)
    return steps, f'{slug} template with the {offer.name} offer', ''


def _preview_sequence(tool_input, report=_noop_report):
    """Show the copy without writing it anywhere. Free."""
    campaign = None
    ref = (tool_input.get('campaign') or '').strip()
    if ref:
        campaign, err = _find_campaign(ref)
        if err:
            return err

    steps, source, err = _steps_from_input(tool_input, campaign=campaign)
    if err:
        return {'previewed': False, 'reason': err}

    from outreach import sequences
    problems = sequences.describe_problems(steps)
    return {
        'previewed': True,
        'campaign': campaign.name if campaign else None,
        'source': source,
        'touches': len(steps),
        'would_pass_preflight': not problems,
        'problems': problems,
        'steps': [
            {
                'touch': i,
                'sends': ('immediately' if i == 1
                          else f"{s.get('delay_days', 3)} days after "
                               f"touch {i - 1}"),
                'subject': s.get('subject') or '(blank — threads under '
                                               'the previous email)',
                'body': s.get('body', ''),
                'words': len(s.get('body', '').split()),
            }
            for i, s in enumerate(steps, 1)
        ],
    }


def _set_campaign_sequence(tool_input, report=_noop_report):
    """Write a sequence into an existing campaign. Runs after approval.

    Pre-flight is not skippable and runs BEFORE anything reaches
    Instantly. describe_problems enforces length, plain text, the
    CAN-SPAM footer and no invented pricing — the rules that decide
    whether copy is fit to put in front of a stranger. A campaign whose
    copy fails them is a campaign somebody eventually starts.
    """
    from outreach import instantly, sequences

    campaign, err = _find_campaign(tool_input.get('campaign'))
    if err:
        return {'updated': False, 'reason': err}
    if not campaign.instantly_campaign_id:
        return {
            'updated': False,
            'reason': (f'{campaign.name} has no Instantly campaign id, so '
                       f'there is nothing to write the sequence into. It '
                       f'exists in Django only. Say so rather than '
                       f'creating a second campaign.'),
        }

    steps, source, err = _steps_from_input(tool_input, campaign=campaign)
    if err:
        return {'updated': False, 'reason': err}

    report(f'composing {len(steps)} touches from {source}')
    problems = sequences.describe_problems(steps)
    if problems:
        report(f'pre-flight FAILED: {len(problems)} problem(s)')
        return {
            'updated': False,
            'reason': 'The copy failed pre-flight; nothing was written.',
            'problems': problems,
        }
    report(f'pre-flight passed — writing to {campaign.name}')

    try:
        instantly.update_campaign_sequence(
            campaign.instantly_campaign_id, steps)
    except Exception as exc:  # noqa: BLE001
        logger.exception('set_campaign_sequence failed')
        return {'updated': False, 'reason': f'Instantly refused: {exc}'}

    if not tool_input.get('steps'):
        offer_key = (tool_input.get('offer') or '').strip()
        if offer_key:
            from outreach.models import Offer
            offer = Offer.objects.filter(key=offer_key).first()
            if offer is not None and campaign.offer_id != offer.pk:
                campaign.offer = offer
                campaign.save(update_fields=['offer', 'updated_at'])

    report(f'{campaign.name} now sends {len(steps)} touches')
    return {
        'updated': True,
        'campaign': campaign.name,
        'touches': len(steps),
        'source': source,
        'active': campaign.active,
        'note': ('The copy is written. This did not start the campaign — '
                 'if it was paused it is still paused.'),
    }


def _create_campaign(tool_input, report=_noop_report):
    """Build one campaign arm. Runs only after approval.

    Two guards that are not negotiable:

    * The copy goes through ``sequences.describe_problems`` BEFORE
      anything is created. Instantly is where copy becomes email; a
      campaign built from copy that fails pre-flight is a campaign
      someone eventually starts.
    * The campaign is created PAUSED and this never activates it. That
      click stays a human one in Instantly's own UI, because it is the
      irreversible step that puts mail in front of strangers.
    """
    from django.utils.text import slugify

    from outreach import instantly, sequences
    from outreach.apify_source import business_type_for_niche
    from outreach.models import Offer, OutreachCampaign

    name = (tool_input.get('name') or '').strip()
    niche = (tool_input.get('niche') or '').strip()
    state = (tool_input.get('state') or '').strip().upper()
    if not (name and niche and state):
        return {'created': False,
                'reason': 'name, niche and state are all required.'}

    slug = slugify(name)[:50]
    if OutreachCampaign.objects.filter(slug=slug).exists():
        return {'created': False,
                'reason': f'A campaign with slug {slug!r} already exists. '
                          f'Use list_campaigns and say what is wrong with '
                          f'it rather than creating a duplicate.'}

    offer_key = (tool_input.get('offer') or sequences.DEFAULT_OFFER).strip()
    offer = Offer.objects.filter(key=offer_key).first()
    if offer is None:
        return {'created': False,
                'reason': f'No offer with key {offer_key!r}. '
                          f'Call list_offers.'}

    sequence = (tool_input.get('sequence') or 'texas-law').strip()
    report(f'building {sequence} copy with the {offer.name} offer')
    try:
        steps = sequences.build_steps(sequence, offer=offer)
    except sequences.SequenceError as exc:
        return {'created': False, 'reason': str(exc)}

    problems = sequences.describe_problems(steps)
    if problems:
        report(f'pre-flight FAILED: {len(problems)} problem(s)')
        return {
            'created': False,
            'reason': 'The copy failed pre-flight and nothing was created.',
            'problems': problems,
        }
    report(f'pre-flight passed — {len(steps)} touches')

    report('creating the campaign in Instantly (paused)')
    try:
        result = instantly.create_campaign(name, steps)
    except Exception as exc:  # noqa: BLE001
        logger.exception('create_campaign failed')
        return {'created': False, 'reason': f'Instantly refused: {exc}'}

    campaign_id = (result or {}).get('id') or (result or {}).get('campaign_id')
    if not campaign_id:
        return {'created': False,
                'reason': f'Instantly returned no campaign id: '
                          f'{str(result)[:200]}'}

    business_type = business_type_for_niche(niche)
    campaign = OutreachCampaign.objects.create(
        name=name, slug=slug, niche=niche, state=state,
        business_type=business_type, offer=offer,
        instantly_campaign_id=campaign_id,
        # Never active on arrival. Assignment will not fill it and push
        # will not touch it until a human turns both this and the
        # Instantly campaign on.
        active=False,
    )
    report(f'created {name} — paused, id {campaign_id}')
    return {
        'created': True,
        'campaign': campaign.name,
        'slug': campaign.slug,
        'instantly_campaign_id': campaign_id,
        'business_type': business_type or '(unconstrained)',
        'active': False,
        'next_step': (
            'The campaign exists but is PAUSED and inactive, so nothing '
            'will be assigned or pushed to it. A human must start it in '
            'Instantly and mark the Django arm active before it can '
            'receive leads.'),
    }


def _push_to_instantly(_input, report=_noop_report):
    """Runs only after approval — see execute_approved."""
    from outreach.tasks import push_to_instantly_task
    return {'result': push_to_instantly_task()}


def _write_journal(tool_input, report=_noop_report, run_obj=None):
    entry = (tool_input.get('entry') or '').strip()
    if not entry:
        return {'saved': False, 'reason': 'entry was empty.'}
    if run_obj is not None:
        run_obj.summary = entry
        run_obj.save(update_fields=['summary'])
        employee = run_obj.employee
        employee.last_journal_entry = entry
        employee.save(update_fields=['last_journal_entry'])
    return {'saved': True, 'characters': len(entry)}


# Tool name -> the name of the function that implements it.
#
# NAMES, not function objects, so the lookup happens at CALL time rather
# than at import. Binding the objects here would freeze whatever existed
# when this module was first imported, which makes the paid and sending
# paths impossible to substitute in a test — and a safety gate that
# cannot be tested is a safety gate nobody has checked. Every test below
# that proves an approved scrape runs exactly once depends on this.
_IMPL = {
    'funnel_status': '_funnel_status',
    'city_progress': '_city_progress',
    'run_pipeline': '_run_pipeline',
    'assign_campaigns': '_assign_campaigns',
    'start_scrape': '_start_scrape',
    'import_dataset': '_import_dataset',
    'push_to_instantly': '_push_to_instantly',
    'create_campaign': '_create_campaign',
    'campaign_stats': '_campaign_stats',
    'preview_sequence': '_preview_sequence',
    'set_campaign_sequence': '_set_campaign_sequence',
    'find_leads': '_find_leads',
    'lead_detail': '_lead_detail',
    'list_campaigns': '_list_campaigns',
    'list_offers': '_list_offers',
    'recent_replies': '_recent_replies',
    'sourcing_history': '_sourcing_history',
    'spend_summary': '_spend_summary',
}


def _resolve(name):
    """The callable implementing ``name``, or None."""
    func_name = _IMPL.get(name)
    return globals().get(func_name) if func_name else None


# ── Progress reporting ────────────────────────────────────────────────
#
# _noop_report lives at the top of the module — it is a default argument
# on every implementation, so it has to exist before they are defined.

def action_reporter(action):
    """A ``report(line)`` bound to one AIEmployeeAction.

    Each line is written straight through rather than buffered: they are
    a handful per tool, they arrive seconds apart, and the whole point is
    that the page can see them WHILE the tool runs. Buffering would make
    them appear all at once at the end, which is the spinner we are
    replacing.

    Failures are swallowed. A progress line is a nicety; losing the
    scrape it was describing is not.
    """
    def report(line):
        text = str(line).strip()
        if not text:
            return
        try:
            type(action).objects.filter(pk=action.pk).update(
                progress=(list(action.progress or []) + [text])[-40:])
            action.progress = (list(action.progress or []) + [text])[-40:]
        except Exception:  # noqa: BLE001
            logger.exception('could not record progress for action %s',
                             action.pk)
    return report


# ── Lookup implementations ────────────────────────────────────────────
#
# These exist so the chat can answer about SPECIFICS. Everything here
# reads; nothing writes, nothing spends.
#
# Results are capped and the cap is reported back in the payload. A tool
# that silently returns the first 25 of 900 lets the model say "we have
# 25 leads" with total confidence, which is worse than saying nothing.


def _clamp(value, default, maximum):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _lead_row(lead):
    """The compact shape find_leads returns — one line per lead."""
    return {
        'id': lead.pk,
        'firm': lead.firm_name,
        'contact': lead.attorney_name or '',
        'email': lead.email or '',
        'city': lead.city or '',
        'state': lead.state or '',
        'business_type': lead.business_type or '',
        'status': lead.status,
        'verification': lead.email_verification_status,
        'score': lead.score,
        'has_icebreaker': bool((lead.icebreaker or '').strip()),
        'campaign': lead.campaign.name if lead.campaign_id else None,
        'pushed': bool(lead.pushed_to_instantly_at),
    }


def _find_leads(tool_input, report=_noop_report):
    from outreach.models import Lead

    limit = _clamp(tool_input.get('limit'), 25, 100)
    qs = Lead.objects.select_related('campaign').all()

    query = (tool_input.get('query') or '').strip()
    if query:
        qs = qs.filter(
            Q(firm_name__icontains=query)
            | Q(attorney_name__icontains=query)
            | Q(email__icontains=query))
    city = (tool_input.get('city') or '').strip()
    if city:
        qs = qs.filter(city__icontains=city)
    state = (tool_input.get('state') or '').strip()
    if state:
        qs = qs.filter(state__icontains=state)
    status = (tool_input.get('status') or '').strip()
    if status:
        qs = qs.filter(status=status)

    has_ice = tool_input.get('has_icebreaker')
    if has_ice is True:
        qs = qs.exclude(icebreaker='').exclude(icebreaker__isnull=True)
    elif has_ice is False:
        qs = qs.filter(Q(icebreaker='') | Q(icebreaker__isnull=True))

    total = qs.count()
    rows = [_lead_row(lead) for lead in qs.order_by('-created_at')[:limit]]
    return {
        'matched': total,
        'returned': len(rows),
        'truncated': total > len(rows),
        'leads': rows,
    }


def _lead_detail(tool_input, report=_noop_report):
    from outreach.models import Lead

    ref = (tool_input.get('lead') or '').strip()
    if not ref:
        return 'Give a lead id, email, or firm name.'

    lead = None
    if ref.isdigit():
        lead = Lead.objects.filter(pk=int(ref)).first()
    if lead is None and '@' in ref:
        lead = Lead.objects.filter(email__iexact=ref).first()
    if lead is None:
        matches = list(Lead.objects.filter(firm_name__icontains=ref)[:5])
        if len(matches) > 1:
            return {
                'ambiguous': True,
                'detail': f'{ref!r} matches several leads — ask by id.',
                'candidates': [
                    {'id': m.pk, 'firm': m.firm_name, 'city': m.city}
                    for m in matches],
            }
        lead = matches[0] if matches else None
    if lead is None:
        return f'No lead matches {ref!r}.'

    row = _lead_row(lead)
    row.update({
        'website': lead.website or '',
        'phone': lead.phone or '',
        'icebreaker': lead.icebreaker or '',
        'icebreaker_written_at': lead.icebreaker_generated_at,
        # The measured facts the icebreaker was allowed to draw on. Given
        # alongside the copy on purpose: judging whether a line is honest
        # is impossible without the numbers it claims to describe.
        'measured': {
            'pagespeed_performance': lead.website_performance_score,
            'pagespeed_mobile': lead.website_mobile_score,
            'pagespeed_seo': lead.website_seo_score,
            'has_ssl': lead.has_ssl,
            'site_status': lead.site_status or 'live',
            'copyright_year': lead.copyright_year,
            'founded_year': lead.founded_year,
            'practice_areas': lead.practice_areas or '',
        },
        'source': lead.source,
        'sequence_step': lead.sequence_step,
        'unsubscribed': lead.unsubscribed,
        'needs_review': lead.needs_review,
        'review_reason': lead.review_reason or '',
        'notes': (lead.notes or '')[:1500],
        'created_at': lead.created_at,
    })
    return row


def _list_campaigns(_input, report=_noop_report):
    from outreach.models import OutreachCampaign

    out = []
    for c in OutreachCampaign.objects.select_related('offer').all():
        assigned = c.leads.count() if hasattr(c, 'leads') else 0
        out.append({
            'name': c.name,
            'slug': c.slug,
            'niche': c.niche,
            'business_type': c.business_type,
            'state': c.state,
            'offer': c.offer.name if c.offer_id else None,
            'active': c.active,
            'instantly_campaign_id': c.instantly_campaign_id or None,
            # Both are required. Saying "inactive" when the real problem
            # is a missing Instantly id sends someone to the wrong switch.
            'pushable': bool(c.active and c.instantly_campaign_id),
            'leads_assigned': assigned,
            'leads_pushed': c.leads_pushed,
        })
    return {'campaigns': out, 'count': len(out)}


def _list_offers(_input, report=_noop_report):
    from outreach.models import Offer

    return {'offers': [
        {
            'key': o.key,
            'name': o.name,
            'active': o.active,
            'appeals_to': o.appeals_to,
            'fulfilment_cost': o.fulfilment_cost,
            'pitch': o.pitch,
            'sends': o.sends,
            'replies': o.replies,
            'positive_replies': o.positive_replies,
            'bookings': o.bookings,
        }
        for o in Offer.objects.all()
    ]}


def _recent_replies(tool_input, report=_noop_report):
    from outreach.models import EmailReply

    limit = _clamp(tool_input.get('limit'), 15, 50)
    qs = EmailReply.objects.select_related('lead').order_by('-received_at')
    total = qs.count()
    rows = []
    for r in qs[:limit]:
        rows.append({
            'lead': r.lead.firm_name if r.lead_id else None,
            'lead_email': r.lead.email if r.lead_id else None,
            'subject': r.subject or '',
            'received_at': r.received_at,
            'classification': r.classification,
            'needs_human': r.needs_human,
            'handled': r.handled,
            'body': (r.body or '')[:600],
            'draft_reply': (r.ai_suggested_reply or '')[:600],
        })
    return {'total': total, 'returned': len(rows), 'replies': rows}


def _sourcing_history(tool_input, report=_noop_report):
    from outreach.models import ApifyRun

    limit = _clamp(tool_input.get('limit'), 10, 30)
    rows = []
    for r in ApifyRun.objects.all()[:limit]:
        rows.append({
            'started_at': r.started_at,
            'status': r.status,
            'label': r.label,
            'requested': r.results_requested,
            'returned': r.results_returned,
            'rejected_by_icp_screen': r.results_rejected,
            'cost_usd': float(r.cost_usd or 0),
            'error': (r.error or '')[:300],
        })
    return {'runs': rows, 'count': len(rows)}


def _spend_summary(_input, report=_noop_report):
    from outreach import spend
    from outreach.apify_source import budget_status
    from outreach.models import OutreachSettings

    cfg = OutreachSettings.load()
    allowed, why = spend.check_spend_allowed()
    apify = budget_status()
    return {
        'claude_today_usd': float(spend.spent_today()),
        'claude_daily_cap_usd': float(spend.daily_cap()),
        'claude_allowed_now': allowed,
        'claude_reason': why,
        'apify_month_to_date_usd': float(apify['spent_usd']),
        'apify_monthly_budget_usd': float(apify['budget_usd']),
        'apify_runs_today': spend.apify_runs_today(),
        'apify_max_runs_per_day': cfg.apify_max_runs_per_day,
    }


# ── The executor ──────────────────────────────────────────────────────

def make_executor(run):
    """Build the ``(name, input) -> result`` callable for one run.

    Every call becomes an AIEmployeeAction row, including the ones that
    are refused. A refusal is the most interesting thing an agent does in
    a week and it must not be the one event that leaves no trace.
    """
    from admin_dashboard.models import AIEmployeeAction

    def execute(name, tool_input):
        kind = tool_kind(name)
        if kind is None:
            return f'No such tool: {name}.'

        if name == 'write_journal':
            action = AIEmployeeAction.objects.create(
                run=run, tool_name=name, tool_input=tool_input)
            result = _write_journal(tool_input, run_obj=run)
            action.result = json.dumps(result)[:4000]
            action.executed_at = timezone.now()
            action.save(update_fields=['result', 'executed_at'])
            return result

        if kind == COMMIT:
            # File it and stop. The model is told plainly that nothing
            # happened, so it does not report the work as done in its
            # journal -- an agent that believes it scraped Houston will
            # move on to Dallas next run and leave Houston unsourced.
            AIEmployeeAction.objects.create(
                run=run, tool_name=name, tool_input=tool_input,
                requires_approval=True,
                result='Awaiting human approval — not executed.')
            return {
                'executed': False,
                'status': 'awaiting_approval',
                'detail': (
                    f'{name} has been filed for human approval and has '
                    f'NOT run. Nothing was charged and nothing was sent. '
                    f'It will execute on a later run if approved. Do not '
                    f'call it again this run, and do not describe this '
                    f'work as done.'),
            }

        action = AIEmployeeAction.objects.create(
            run=run, tool_name=name, tool_input=tool_input)
        try:
            result = _resolve(name)(tool_input, action_reporter(action))
        except Exception as exc:  # noqa: BLE001
            logger.exception('tool %r failed', name)
            action.result = f'FAILED: {exc}'[:4000]
            action.executed_at = timezone.now()
            action.save(update_fields=['result', 'executed_at'])
            return f'Tool {name} failed: {exc}'

        action.result = json.dumps(result, default=str)[:4000]
        action.executed_at = timezone.now()
        action.save(update_fields=['result', 'executed_at'])
        return result

    return execute


def execute_approved():
    """Run the COMMIT calls a human approved since the last run.

    Called at the START of a run, before the model is given the funnel,
    so its first read reflects the approved work rather than the state
    before it.

    ``executed_at__isnull=True`` is what makes this idempotent. Approval
    is permanent, so without it every wake-up would re-run every scrape
    ever approved.
    """
    from admin_dashboard.models import AIEmployeeAction

    pending = AIEmployeeAction.objects.filter(
        requires_approval=True, approved=True, executed_at__isnull=True)

    done = []
    for action in pending:
        impl = _resolve(action.tool_name)
        if impl is None:
            action.result = f'No implementation for {action.tool_name}.'
            action.executed_at = timezone.now()
            action.save(update_fields=['result', 'executed_at'])
            continue
        try:
            result = impl(action.tool_input)
            action.result = json.dumps(result, default=str)[:4000]
        except Exception as exc:  # noqa: BLE001
            logger.exception('approved tool %r failed', action.tool_name)
            action.result = f'FAILED: {exc}'[:4000]
            result = {'error': str(exc)}

        # Stamped even on failure. A scrape that errored halfway may
        # still have been charged, so re-running it is not obviously
        # safer than leaving it -- and a silent retry loop on a paid tool
        # is the worse of the two failures.
        action.executed_at = timezone.now()
        action.save(update_fields=['result', 'executed_at'])
        done.append({'tool': action.tool_name, 'result': result})

    return done
