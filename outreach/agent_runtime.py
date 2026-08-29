"""
Waking Prospect up — §5.4.

One entry point, ``run_prospect``, called from a Celery beat entry or the
"Wake now" button. Everything it needs to be safe is already built and is
deliberately NOT reimplemented here:

    spend.check_spend_allowed()   the daily dollar cap
    instantly.sending_allowed()   the warmup + switch gates on real sends
    agent_tools COMMIT class      human approval on money and mail
    copy_guard                    what may appear in an email body

This module's own job is narrow: decide whether a run may start, build
the prompt, run the loop, and write down what happened.

ORDER OF OPERATIONS, AND WHY
----------------------------
Approved COMMIT calls execute BEFORE the model is woken, not after. If a
human approved a Houston scrape yesterday, Prospect's first look at the
funnel should already include those leads — otherwise it reads a stale
picture and asks for the same scrape a second time.

WHAT KEEPS A RUN FROM ETERNITY
------------------------------
``max_steps`` bounds tool calls, ``check_spend_allowed`` bounds dollars,
and both the Apify and Instantly paths bound themselves. A run that hits
any ceiling still returns and still writes a journal entry, because a run
that spent money and then raised would leave no record of what it bought.
"""

import logging

from django.utils import timezone

from outreach import spend

logger = logging.getLogger(__name__)

PROSPECT_SLUG = 'prospect'

# Ten tool calls is enough for: status, city progress, pipeline, assign,
# a dry-run re-check, a commit request, journal -- with headroom. It is
# not enough to loop on a failing tool for an hour.
MAX_STEPS = 10


# Everything true of Prospect regardless of HOW it was woken. Split out
# so the scheduled run and the chat pane cannot drift into two agents
# with different rules about what they may claim — only the closing
# instructions differ, and they are appended below.
IDENTITY = """\
You are Prospect, the cold-outreach operator for Aspired Websites LLC — a
web design agency run by Zachery Long, selling to law firms and small
businesses in Texas and Georgia.

YOUR JOB
You keep the outreach funnel moving and you decide what it needs next.
The mechanical stages (verify, enrich, personalise, assign, push) already
run on a schedule without you. You exist for the judgement calls that
schedule cannot make:

  - Is the current city exhausted, or did the last scrape under-deliver?
  - Are leads piling up ready-but-unassigned? That means a campaign is
    missing or full, and it is a silent failure — nothing else reports it.
  - Has an A/B arm collected enough leads for its reply rate to mean
    anything, or is it still noise?
  - Is anything stuck for a reason a human needs to hear about?

GEOGRAPHY
Work Texas first, then Georgia. Finish a city before starting another:
source it, run it through the pipeline, and only move on when almost
nothing is left unprocessed. Roughly 1,000 raw leads per city is the
target.

WHAT YOU MUST NOT DO
  - Never claim work is done that only got filed for approval. When a
    tool tells you it is awaiting approval, nothing happened. Say so.
  - Never ask for a second scrape while one is awaiting approval.
  - Never guess at numbers. Call funnel_status and read them.
  - Never describe a lead, a firm, or a website you have not measured.

STATISTICS, BRIEFLY
An A/B arm needs roughly 300-400 leads before its reply rate can be
distinguished from another arm's. Below that, a difference is noise. If
asked which offer is winning and the arms are small, the honest answer is
"not enough data yet" — say that rather than ranking noise.

Be brief and concrete. Zachery reads these; he does not need padding.\
"""


SYSTEM_PROMPT = IDENTITY + """

HOW TO FINISH
Call write_journal exactly once, last. It is your only memory: you will
not remember this session next time. Write the state of play — which city
is in progress, what you are waiting on, what you would do next and why —
not a list of the tools you called. Then stop.\
"""


def _employee():
    from admin_dashboard.models import AIEmployee
    return AIEmployee.objects.filter(slug=PROSPECT_SLUG).first()


def _build_kickoff(employee):
    """The user turn: last run's memory, open tasks, and today's date."""
    parts = [
        f'Today is {timezone.now():%A %d %B %Y}. This is a scheduled '
        f'run — decide what the funnel needs and do it.',
    ]

    if employee.last_journal_entry:
        parts.append(
            '\nYour journal from your previous run:\n'
            f'"""\n{employee.last_journal_entry.strip()}\n"""')
    else:
        parts.append(
            '\nYou have no journal from a previous run — this is either '
            'your first run or the first since the runtime was built. '
            'Start by reading the funnel.')

    from admin_dashboard.models import AIEmployeeTask
    tasks = list(AIEmployeeTask.objects.filter(
        employee=employee, status='pending').order_by('created_at')[:10])
    if tasks:
        lines = '\n'.join(f'  [{t.pk}] {t.instruction}' for t in tasks)
        parts.append(
            f'\nZachery has assigned you these tasks. They take priority '
            f'over routine work:\n{lines}')

    return '\n'.join(parts)


def run_prospect(trigger='scheduled'):
    """Wake Prospect for one cycle. Returns a short status string.

    Never raises for an operational reason — a disabled agent, an
    exhausted budget and a missing API key are all *answers*, returned as
    text, because this is called from a Celery beat entry where a raise
    becomes a stack trace nobody reads.
    """
    from admin_dashboard.models import AIEmployeeRun
    from outreach.agent_tools import (
        anthropic_tools, execute_approved, make_executor,
    )
    from reporting import ai

    employee = _employee()
    if employee is None:
        return f'No AIEmployee with slug "{PROSPECT_SLUG}".'

    if trigger == 'scheduled' and not employee.active:
        # Manual wake still works while paused -- pausing is meant to
        # stop the schedule, not to lock Zach out of his own agent.
        return f'{employee.name} is paused; no scheduled run.'

    if not ai.is_configured():
        return 'ANTHROPIC_API_KEY is not set — cannot run.'

    allowed, why = spend.check_spend_allowed()
    if not allowed:
        return f'Not running: {why}'

    # Approved work first, so the funnel the model reads is current.
    try:
        executed = execute_approved()
    except Exception:  # noqa: BLE001
        logger.exception('execute_approved failed')
        executed = []

    run = AIEmployeeRun.objects.create(employee=employee, trigger=trigger)

    def on_usage(model, input_tokens, output_tokens):
        """Bill incrementally so a crashed run still counts its spend."""
        cost = spend.claude_call_cost_usd(model, input_tokens, output_tokens)
        run.spend_usd = (run.spend_usd or 0) + cost
        run.save(update_fields=['spend_usd'])

    kickoff = _build_kickoff(employee)
    if executed:
        done = '; '.join(
            f"{e['tool']}" for e in executed)
        kickoff += (
            f'\n\nBefore this run started, work you had filed for '
            f'approval was approved and has now been EXECUTED: {done}. '
            f'The funnel numbers below already include it.')

    try:
        result = ai.claude_agent_loop(
            system=SYSTEM_PROMPT,
            tools=anthropic_tools(),
            tool_executor=make_executor(run),
            user_message=kickoff,
            model=ai.MODEL_CONTENT,
            max_steps=MAX_STEPS,
            effort=employee.reasoning_effort,
            on_usage=on_usage,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('run_prospect: agent loop failed')
        run.status = 'failed'
        run.summary = f'Run failed: {exc}'
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'summary', 'finished_at'])
        return f'failed: {exc}'

    run.steps_used = result.get('steps_used', 0)
    run.message_history = result.get('messages', [])
    run.finished_at = timezone.now()
    run.status = ('completed' if result.get('stopped_reason') == 'done'
                  else 'failed')

    # write_journal sets run.summary. If the model finished without
    # calling it, fall back to its closing text rather than leaving the
    # run log blank -- a run with no summary reads as a run that did
    # nothing, which is a different and more alarming thing.
    if not run.summary:
        run.summary = (result.get('final_text')
                       or f"Ended: {result.get('stopped_reason')}")[:8000]

    run.save(update_fields=[
        'steps_used', 'message_history', 'finished_at', 'status', 'summary'])

    _close_completed_tasks(employee, run)

    return (f"{run.status} — {run.steps_used} steps, "
            f"${run.spend_usd:.4f}, {len(executed)} approved action(s) run")


def _close_completed_tasks(employee, run):
    """Move assigned tasks to in_progress once a run has seen them.

    Deliberately NOT marked done. Nothing here can verify that a free-text
    instruction was actually carried out, and a task that silently marks
    itself complete is worse than one that stays open — the second gets
    noticed.
    """
    from admin_dashboard.models import AIEmployeeTask
    AIEmployeeTask.objects.filter(
        employee=employee, status='pending').update(status='in_progress')
