"""
Daily spend ceilings for the AI agents (COLD_OUTREACH_AGENT.md §1.3).

TWO SEPARATE CAPS
-----------------
Claude and Apify bill on different shapes and get independent gates:

  * Claude / LLM — dollars, accumulating smoothly per token.
    ``check_spend_allowed()``.
  * Apify — bounded by RUN COUNT and RESULT COUNT, not dollars, because
    one bad actor call can burn a lot in a single request and the agent
    cannot reliably predict compute cost up front.
    ``check_apify_allowed()``.

They must never draw from the same pool. A runaway scrape that silently
ate the reasoning budget would take Prospect offline for the rest of the
day with no obvious cause.

This is a HARD BLOCK, not a prompt instruction. The agent is told its
remaining budget so it can wrap up gracefully, but the money-spending
tools stop executing regardless of what the model decides to do.

WHERE THE NUMBER COMES FROM
---------------------------
``AIEmployeeRun.spend_usd``, summed across every run that STARTED today.

Not ``reporting.models.ClaudeUsage`` — that is a per-month, per-model
rollup (``unique_together = ['year_month', 'model']``, roughly three rows
a month, incremented via ``F()``). It cannot answer "how much has been
spent today", which is exactly the question a daily cap asks. The two
coexist: ClaudeUsage stays the org-wide monthly view for the dashboard
widget; this module is the run-scoped ledger the guardrail reads.

Runs write ``spend_usd`` incrementally as they go, so a run that is still
in flight — or one that crashed halfway — still counts against the day.
That is deliberate: a cap that only counted finished runs could be
walked straight past by one long run.
"""

import datetime
import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


# Claude prices are per million tokens; Apify costs arrive already in USD.
_TOKENS_PER_MTOK = Decimal(1_000_000)


def spent_today(now=None):
    """Total USD spent by every AI employee run started today.

    Returns Decimal('0') when the models are unavailable (fresh checkout
    before migrations) so the caller degrades to "nothing spent yet"
    rather than exploding — the cap itself still applies on the next
    call once migrations land.
    """
    try:
        from admin_dashboard.models import AIEmployeeRun
    except Exception:  # noqa: BLE001
        return Decimal('0')

    today = (now or timezone.now()).astimezone(
        timezone.get_current_timezone()).date()
    start = timezone.make_aware(
        datetime.datetime.combine(today, datetime.time.min),
        timezone.get_current_timezone())
    end = start + datetime.timedelta(days=1)

    try:
        total = AIEmployeeRun.objects.filter(
            started_at__gte=start, started_at__lt=end,
        ).aggregate(total=Sum('spend_usd'))['total']
    except Exception:  # noqa: BLE001
        logger.exception('spent_today: could not read AIEmployeeRun')
        return Decimal('0')
    return total or Decimal('0')


def daily_cap():
    """The configured ceiling as a Decimal. 0 means 'spend nothing'."""
    from outreach.models import OutreachSettings
    return Decimal(str(OutreachSettings.load().daily_ai_spend_cap_usd or 0))


def remaining_budget(now=None):
    """USD left for today. Never negative."""
    left = daily_cap() - spent_today(now=now)
    return left if left > 0 else Decimal('0')


def check_spend_allowed(now=None):
    """Gate for any tool that is about to cost money.

    Returns ``(allowed: bool, reason: str)``. ``reason`` is written to be
    handed straight back to the model as a tool result — it tells the
    agent to wrap up rather than leaving it to guess why a tool failed.
    """
    cap = daily_cap()
    if cap <= 0:
        return False, (
            'Spend cap reached: the daily AI spend cap is set to $0, so no '
            'paid tools may run. Finish up and write your journal entry.')

    spent = spent_today(now=now)
    if spent >= cap:
        return False, (
            f'Spend cap reached: ${spent:.2f} of ${cap:.2f} used today. '
            f'No further paid tools will run. Wrap up the work you have '
            f'already done and write your journal entry.')
    return True, ''


# ── Apify quota (separate pool — see module docstring) ────────────────
#
# The run ledger lands with §3 (outreach/apify_source.py). Until then
# apify_runs_today() reports 0 and the gate is permissive on count while
# still honouring a 0 setting, so nothing can start scraping by accident
# before the ledger exists to bound it.

def apify_runs_today(now=None):
    """Apify actor runs started today. 0 until the §3 ledger exists."""
    try:
        from outreach.models import ApifyRun  # §3 builds this
    except ImportError:
        return 0

    today = (now or timezone.now()).astimezone(
        timezone.get_current_timezone()).date()
    start = timezone.make_aware(
        datetime.datetime.combine(today, datetime.time.min),
        timezone.get_current_timezone())
    try:
        return ApifyRun.objects.filter(
            started_at__gte=start,
            started_at__lt=start + datetime.timedelta(days=1),
        ).count()
    except Exception:  # noqa: BLE001
        logger.exception('apify_runs_today: could not read ApifyRun')
        return 0


def apify_max_results_per_run():
    """Per-run result ceiling — bounds the cost of any single run."""
    from outreach.models import OutreachSettings
    return max(0, int(OutreachSettings.load().apify_max_results_per_run or 0))


def check_apify_allowed(now=None):
    """Gate for starting an Apify actor run.

    Returns ``(allowed: bool, reason: str)``. ``reason`` is written to be
    handed back to the model verbatim — "quota reached" so it wraps up
    cleanly, rather than an error it might read as something to retry.

    Deliberately independent of the Claude cap: exhausting one must never
    disable the other.
    """
    from outreach.models import OutreachSettings

    cap = max(0, int(OutreachSettings.load().apify_max_runs_per_day or 0))
    if cap <= 0:
        return False, (
            'Apify quota reached: lead sourcing is disabled (max runs per '
            'day is 0). Work with the leads already in the database.')

    used = apify_runs_today(now=now)
    if used >= cap:
        return False, (
            f'Apify quota reached: {used} of {cap} actor runs used today. '
            f'No further sourcing runs will start. Work with the leads '
            f'already in the database and wrap up.')
    return True, ''


def claude_call_cost_usd(model, input_tokens, output_tokens):
    """USD cost of one Claude call, using the same rate table the AI
    Usage widget reads.

    Returns Decimal('0') for a model missing from the table — and logs
    loudly, because an unpriced model silently reads as free and would
    let the cap be walked past. Keep
    ``reporting.models.CLAUDE_PRICING_USD_PER_MTOK`` in sync whenever a
    model constant changes.
    """
    from reporting.models import CLAUDE_PRICING_USD_PER_MTOK

    rates = CLAUDE_PRICING_USD_PER_MTOK.get(model)
    if not rates:
        logger.error(
            'claude_call_cost_usd: no rate for model %r — this call is '
            'being counted as $0.00 and the daily spend cap will '
            'under-report. Add it to CLAUDE_PRICING_USD_PER_MTOK.', model)
        return Decimal('0')

    return (
        (Decimal(int(input_tokens or 0)) / _TOKENS_PER_MTOK)
        * Decimal(str(rates['input']))
        + (Decimal(int(output_tokens or 0)) / _TOKENS_PER_MTOK)
        * Decimal(str(rates['output']))
    )
