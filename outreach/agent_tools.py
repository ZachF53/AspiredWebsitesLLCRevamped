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

WHY APPROVAL IS NOT EXECUTION
-----------------------------
``ai_action_decide`` records the answer and returns. It does not run the
tool. A paid Apify scrape inside a request/response cycle would block the
worker, and a timeout would leave "did it charge me?" unanswerable. The
approved row is picked up at the start of the next run, executed once,
and stamped with ``executed_at`` — which is also what stops a scrape the
operator approved once from re-running on every subsequent wake-up.
"""

import json
import logging

from django.utils import timezone

from outreach import spend

logger = logging.getLogger(__name__)

READ, ACT, COMMIT = 'read', 'act', 'commit'


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
            'files a request and returns immediately; the scrape happens '
            'after a human approves, on the next run. Only request this '
            'when the current city is genuinely exhausted. Requesting a '
            'second scrape while one is already awaiting approval is '
            'wasteful; check funnel_status first.'),
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
        'name': 'push_to_instantly',
        'kind': COMMIT,
        'description': (
            'Push assigned leads into their Instantly campaigns, which '
            'queues REAL EMAILS TO REAL STRANGERS. Requires human '
            'approval. This is also gated independently by the warmup '
            'check and the sending switch — if mailboxes are not warm '
            'enough, approval alone will not send. Say in `reason` how '
            'many leads and which arms.'),
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
]

_BY_NAME = {t['name']: t for t in TOOLS}


def anthropic_tools():
    """TOOLS minus our own bookkeeping, in the shape the API expects."""
    return [{k: v for k, v in t.items() if k != 'kind'} for t in TOOLS]


def tool_kind(name):
    tool = _BY_NAME.get(name)
    return tool['kind'] if tool else None


# ── Implementations ───────────────────────────────────────────────────

def _funnel_status(_input):
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


def _city_progress(tool_input):
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


def _run_pipeline(_input):
    from outreach.tasks import (
        assign_campaigns_task, enrich_pending_leads_task,
        generate_icebreakers_task, verify_leads_task,
    )
    allowed, why = spend.check_spend_allowed()
    if not allowed:
        return {'ran': False, 'reason': why}

    return {
        'ran': True,
        'verify_pre': verify_leads_task(),
        'enrich': enrich_pending_leads_task(),
        'verify_post': verify_leads_task(),
        'icebreakers': generate_icebreakers_task(),
        'assign': assign_campaigns_task(),
    }


def _assign_campaigns(tool_input):
    from outreach.assignment import assign_leads
    return assign_leads(dry_run=bool(tool_input.get('dry_run')))


def _start_scrape(tool_input):
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

    try:
        leads, ledger = run_lead_search(
            niche=niche, city=city, state=state, max_results=limit,
            label=f'{niche} in {city}, {state} (Prospect)')
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

    result = import_leads(leads, source='apify')
    result['apify_run_id'] = ledger.apify_run_id
    result['cost_usd'] = float(ledger.actual_cost_usd or 0)
    result['scraped'] = True
    return result


def _import_dataset(tool_input):
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


def _push_to_instantly(_input):
    """Runs only after approval — see execute_approved."""
    from outreach.tasks import push_to_instantly_task
    return {'result': push_to_instantly_task()}


def _write_journal(tool_input, run=None):
    entry = (tool_input.get('entry') or '').strip()
    if not entry:
        return {'saved': False, 'reason': 'entry was empty.'}
    if run is not None:
        run.summary = entry
        run.save(update_fields=['summary'])
        employee = run.employee
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
}


def _resolve(name):
    """The callable implementing ``name``, or None."""
    func_name = _IMPL.get(name)
    return globals().get(func_name) if func_name else None


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
            result = _write_journal(tool_input, run=run)
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
            result = _resolve(name)(tool_input)
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
