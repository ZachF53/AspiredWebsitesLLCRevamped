"""
Tests for campaign assignment and the Prospect runtime.

The assignment tests cover a bug that produced NO error: push selected
`Lead.objects.filter(campaign=campaign)` and nothing ever set that field,
so the pipeline reported "nothing ready" forever while looking healthy.
The first test below is the regression that would have caught it.

The runtime tests concentrate almost entirely on the approval gate,
because that is the only thing standing between an agent decision and a
real charge or a real email to a stranger. Everything else the runtime
does is recoverable.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from admin_dashboard.models import (
    AIEmployee, AIEmployeeAction, AIEmployeeRun,
)
from outreach import agent_tools, verify
from outreach.assignment import (
    assign_leads, assignable_leads, eligible_campaigns, open_campaigns,
)
from outreach.models import Lead, Offer, OutreachCampaign


def make_lead(**kw):
    defaults = {
        'firm_name': 'Alvarez Law', 'email': 'maria@alvarezlaw.com',
        'city': 'Houston', 'state': 'TX', 'business_type': 'Law Firm',
        'icebreaker': 'Saw you handle immigration work out of Houston.',
        'email_verification_status': verify.VALID,
        'source': 'apify',
    }
    defaults.update(kw)
    return Lead.objects.create(**defaults)


def make_campaign(name='TX Law — Security Review', **kw):
    defaults = {
        'name': name, 'slug': name.lower().replace(' ', '-')[:140],
        'niche': 'law', 'business_type': 'Law Firm', 'state': 'TX',
        'instantly_campaign_id': f'iid-{name[:12]}', 'active': True,
    }
    defaults.update(kw)
    return OutreachCampaign.objects.create(**defaults)


class AssignmentTests(TestCase):

    def test_a_ready_lead_gets_a_campaign(self):
        """The regression. Before assignment existed this stayed None
        forever, and push_to_instantly_task selected an empty set."""
        campaign = make_campaign()
        lead = make_lead()
        self.assertIsNone(lead.campaign)

        assign_leads()

        lead.refresh_from_db()
        self.assertEqual(lead.campaign_id, campaign.pk)

    def test_push_query_finds_the_lead_after_assignment(self):
        """Assignment is only correct if it satisfies the exact filter
        push uses — otherwise we have moved the silent failure, not
        fixed it."""
        campaign = make_campaign()
        make_lead()
        assign_leads()

        found = Lead.objects.filter(
            campaign=campaign, instantly_lead_id='', unsubscribed=False
        ).exclude(icebreaker='').exclude(email='')
        self.assertEqual(found.count(), 1)

    def test_arms_fill_evenly(self):
        """Balanced, not random: a lopsided split at small n makes one
        offer look better than it is."""
        make_campaign('TX Law — A')
        make_campaign('TX Law — B')
        for i in range(10):
            make_lead(email=f'a{i}@firm.com', firm_name=f'Firm {i}')

        summary = assign_leads()

        self.assertEqual(summary['assigned'], 10)
        self.assertEqual(sorted(summary['by_campaign'].values()), [5, 5])

    def test_lead_target_closes_an_arm(self):
        small = make_campaign('TX Law — A', lead_target=2)
        big = make_campaign('TX Law — B')
        for i in range(6):
            make_lead(email=f'a{i}@firm.com', firm_name=f'Firm {i}')

        assign_leads()

        self.assertEqual(small.leads.count(), 2)
        self.assertEqual(big.leads.count(), 4)
        self.assertTrue(small.is_full)
        self.assertFalse(small.accepts_leads)

    def test_wrong_state_is_never_assigned(self):
        """The Los Angeles staffing firm problem: the icebreaker and the
        template were each correct and described different businesses."""
        make_campaign(state='TX')
        lead = make_lead(state='CA', city='Los Angeles')

        summary = assign_leads()

        lead.refresh_from_db()
        self.assertIsNone(lead.campaign)
        self.assertEqual(summary['skipped_no_campaign'], 1)
        self.assertIn('no campaign targets state CA', summary['reasons'])

    def test_inbound_leads_are_never_assigned(self):
        """They contacted us. Cold outreach back would be indefensible."""
        make_campaign()
        lead = make_lead(source='contact_form')

        assign_leads()

        lead.refresh_from_db()
        self.assertIsNone(lead.campaign)

    def test_lead_without_icebreaker_is_not_ready(self):
        make_campaign()
        make_lead(icebreaker='')
        self.assertEqual(assignable_leads().count(), 0)

    def test_flagged_for_review_is_held(self):
        make_campaign()
        make_lead(needs_review=True, review_reason='Name says "recruiting"')
        self.assertEqual(assignable_leads().count(), 0)

    def test_paused_campaign_accepts_nothing(self):
        make_campaign(active=False)
        make_lead()
        self.assertEqual(open_campaigns(), [])
        self.assertEqual(assign_leads()['assigned'], 0)

    def test_campaign_without_instantly_id_accepts_nothing(self):
        make_campaign(instantly_campaign_id='')
        make_lead()
        self.assertEqual(open_campaigns(), [])

    def test_dry_run_writes_nothing(self):
        make_campaign()
        lead = make_lead()

        summary = assign_leads(dry_run=True)

        self.assertEqual(summary['assigned'], 1)
        lead.refresh_from_db()
        self.assertIsNone(lead.campaign)

    def test_eligible_campaigns_uses_the_push_gate(self):
        """Assignment and push must agree on segment matching. Two
        implementations would eventually disagree, and disagreement means
        a mismatched email reaching a real person."""
        tx = make_campaign('TX Law', state='TX')
        make_campaign('GA Law', state='GA')
        lead = make_lead(state='TX')

        self.assertEqual([c.pk for c in eligible_campaigns(lead)], [tx.pk])


class ToolRegistryTests(TestCase):

    def test_every_tool_has_an_implementation(self):
        """A tool advertised to the model with no implementation is a
        guaranteed mid-run failure."""
        for tool in agent_tools.TOOLS:
            if tool['name'] == 'write_journal':
                continue  # handled inline — it needs the run object
            self.assertIn(tool['name'], agent_tools._IMPL,
                          f"{tool['name']} is advertised but not implemented")

    def test_anthropic_payload_drops_our_bookkeeping(self):
        """'kind' is ours. Sending it would be rejected by the API."""
        for tool in agent_tools.anthropic_tools():
            self.assertNotIn('kind', tool)
            self.assertEqual(
                set(tool), {'name', 'description', 'input_schema'})

    def test_money_and_mail_are_commit_class(self):
        self.assertEqual(agent_tools.tool_kind('start_scrape'),
                         agent_tools.COMMIT)
        self.assertEqual(agent_tools.tool_kind('push_to_instantly'),
                         agent_tools.COMMIT)

    def test_reads_are_never_commit(self):
        for name in ('funnel_status', 'city_progress'):
            self.assertEqual(agent_tools.tool_kind(name), agent_tools.READ)


class ApprovalGateTests(TestCase):
    """The gate between an agent decision and a real charge or send."""

    def setUp(self):
        # get_or_create: migration 0006 already seeds this row, so
        # create() collides on the unique slug.
        self.employee, _ = AIEmployee.objects.get_or_create(
            slug='prospect', defaults={'name': 'Prospect'})
        self.run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='manual')
        self.execute = agent_tools.make_executor(self.run)

    def test_scrape_does_not_run_when_asked(self):
        with patch('outreach.agent_tools._start_scrape') as impl:
            result = self.execute('start_scrape', {
                'city': 'Dallas', 'state': 'TX', 'reason': 'Houston done'})

        impl.assert_not_called()
        self.assertFalse(result['executed'])
        self.assertEqual(result['status'], 'awaiting_approval')

    def test_push_does_not_run_when_asked(self):
        with patch('outreach.agent_tools._push_to_instantly') as impl:
            result = self.execute('push_to_instantly', {'reason': '40 leads'})

        impl.assert_not_called()
        self.assertEqual(result['status'], 'awaiting_approval')

    def test_the_model_is_told_plainly_that_nothing_happened(self):
        """An agent that believes it scraped Houston moves on to Dallas
        and leaves Houston unsourced."""
        result = self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})
        detail = result['detail'].lower()
        self.assertIn('not', detail)
        self.assertIn('approval', detail)
        self.assertIn('nothing was charged', detail)

    def test_a_commit_request_files_for_approval(self):
        self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})

        action = AIEmployeeAction.objects.get(tool_name='start_scrape')
        self.assertTrue(action.requires_approval)
        self.assertIsNone(action.approved)
        self.assertIsNone(action.executed_at)

    def test_approved_work_runs_on_the_next_run(self):
        self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})
        action = AIEmployeeAction.objects.get(tool_name='start_scrape')
        action.approved = True
        action.approved_at = timezone.now()
        action.save()

        with patch('outreach.agent_tools._start_scrape',
                   return_value={'scraped': True, 'imported': 40}) as impl:
            done = agent_tools.execute_approved()

        impl.assert_called_once()
        self.assertEqual(len(done), 1)
        action.refresh_from_db()
        self.assertIsNotNone(action.executed_at)

    def test_an_approved_scrape_runs_exactly_once(self):
        """Approval is permanent. Without executed_at, every wake-up
        would re-run it and charge the card again."""
        self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})
        action = AIEmployeeAction.objects.get(tool_name='start_scrape')
        action.approved = True
        action.save()

        with patch('outreach.agent_tools._start_scrape',
                   return_value={'scraped': True}) as impl:
            agent_tools.execute_approved()
            agent_tools.execute_approved()
            agent_tools.execute_approved()

        self.assertEqual(impl.call_count, 1)

    def test_a_rejected_scrape_never_runs(self):
        self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})
        action = AIEmployeeAction.objects.get(tool_name='start_scrape')
        action.approved = False
        action.save()

        with patch('outreach.agent_tools._start_scrape') as impl:
            agent_tools.execute_approved()

        impl.assert_not_called()

    def test_a_failed_approved_scrape_is_not_retried(self):
        """It may already have been charged. A silent retry loop on a
        paid tool is worse than leaving it alone."""
        self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})
        action = AIEmployeeAction.objects.get(tool_name='start_scrape')
        action.approved = True
        action.save()

        with patch('outreach.agent_tools._start_scrape',
                   side_effect=RuntimeError('apify exploded')) as impl:
            agent_tools.execute_approved()
            agent_tools.execute_approved()

        self.assertEqual(impl.call_count, 1)
        action.refresh_from_db()
        self.assertIn('FAILED', action.result)

    def test_act_tools_run_immediately(self):
        make_campaign()
        make_lead()

        result = self.execute('assign_campaigns', {})

        self.assertEqual(result['assigned'], 1)
        action = AIEmployeeAction.objects.get(tool_name='assign_campaigns')
        self.assertFalse(action.requires_approval)
        self.assertIsNotNone(action.executed_at)

    def test_a_crashing_tool_becomes_a_result_not_an_exception(self):
        """A raise would throw away every step the run had completed."""
        with patch('outreach.agent_tools._funnel_status',
                   side_effect=RuntimeError('db gone')):
            result = self.execute('funnel_status', {})

        self.assertIn('failed', str(result).lower())

    def test_an_unknown_tool_is_reported_not_raised(self):
        self.assertIn('No such tool', self.execute('nonsense', {}))

    def test_every_call_leaves_a_row(self):
        """Including refusals — the most interesting event of the week
        must not be the one that leaves no trace."""
        self.execute('start_scrape', {
            'city': 'Dallas', 'state': 'TX', 'reason': 'x'})
        self.execute('funnel_status', {})
        self.assertEqual(self.run.actions.count(), 2)


class JournalTests(TestCase):

    def setUp(self):
        # get_or_create: migration 0006 already seeds this row, so
        # create() collides on the unique slug.
        self.employee, _ = AIEmployee.objects.get_or_create(
            slug='prospect', defaults={'name': 'Prospect'})
        self.run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='manual')

    def test_journal_persists_to_both_run_and_employee(self):
        """The run keeps its own record; the employee carries memory
        into the next run."""
        execute = agent_tools.make_executor(self.run)
        execute('write_journal', {'entry': 'Houston is 80% processed.'})

        self.run.refresh_from_db()
        self.employee.refresh_from_db()
        self.assertEqual(self.run.summary, 'Houston is 80% processed.')
        self.assertEqual(
            self.employee.last_journal_entry, 'Houston is 80% processed.')

    def test_empty_journal_is_refused(self):
        execute = agent_tools.make_executor(self.run)
        result = execute('write_journal', {'entry': '   '})
        self.assertFalse(result['saved'])


class RuntimeGuardTests(TestCase):

    def setUp(self):
        self.employee, _ = AIEmployee.objects.get_or_create(
            slug='prospect', defaults={'name': 'Prospect'})
        self.employee.active = True
        self.employee.save()

    def test_paused_agent_skips_scheduled_runs(self):
        from outreach.agent_runtime import run_prospect
        self.employee.active = False
        self.employee.save()

        with patch('reporting.ai.claude_agent_loop') as loop:
            result = run_prospect(trigger='scheduled')

        loop.assert_not_called()
        self.assertIn('paused', result)
        self.assertEqual(AIEmployeeRun.objects.count(), 0)

    def test_paused_agent_can_still_be_woken_by_hand(self):
        """Pausing stops the schedule; it must not lock Zach out."""
        from outreach.agent_runtime import run_prospect
        self.employee.active = False
        self.employee.save()

        with patch('reporting.ai.is_configured', return_value=True), \
             patch('outreach.spend.check_spend_allowed',
                   return_value=(True, '')), \
             patch('reporting.ai.claude_agent_loop',
                   return_value={'steps_used': 1, 'messages': [],
                                 'final_text': 'ok',
                                 'stopped_reason': 'done'}) as loop:
            run_prospect(trigger='manual')

        loop.assert_called_once()

    def test_exhausted_budget_stops_the_run_before_it_starts(self):
        from outreach.agent_runtime import run_prospect

        with patch('reporting.ai.is_configured', return_value=True), \
             patch('outreach.spend.check_spend_allowed',
                   return_value=(False, 'Spend cap reached: $5 of $5.')), \
             patch('reporting.ai.claude_agent_loop') as loop:
            result = run_prospect()

        loop.assert_not_called()
        self.assertIn('Spend cap reached', result)
        self.assertEqual(AIEmployeeRun.objects.count(), 0)

    def test_missing_api_key_is_an_answer_not_a_crash(self):
        """This runs from a beat entry, where a raise is a stack trace
        nobody reads."""
        from outreach.agent_runtime import run_prospect

        with patch('reporting.ai.is_configured', return_value=False):
            result = run_prospect()

        self.assertIn('ANTHROPIC_API_KEY', result)

    def test_a_failing_loop_still_closes_the_run(self):
        from outreach.agent_runtime import run_prospect

        with patch('reporting.ai.is_configured', return_value=True), \
             patch('outreach.spend.check_spend_allowed',
                   return_value=(True, '')), \
             patch('reporting.ai.claude_agent_loop',
                   side_effect=RuntimeError('api down')):
            result = run_prospect()

        self.assertIn('failed', result)
        run = AIEmployeeRun.objects.get()
        self.assertEqual(run.status, 'failed')
        self.assertIsNotNone(run.finished_at)

    def test_a_run_without_a_journal_still_gets_a_summary(self):
        """A blank summary reads as 'did nothing', which is a different
        and more alarming thing than 'forgot to write it down'."""
        from outreach.agent_runtime import run_prospect

        with patch('reporting.ai.is_configured', return_value=True), \
             patch('outreach.spend.check_spend_allowed',
                   return_value=(True, '')), \
             patch('reporting.ai.claude_agent_loop',
                   return_value={'steps_used': 2, 'messages': [],
                                 'final_text': 'Read the funnel; nothing '
                                               'needed doing.',
                                 'stopped_reason': 'done'}):
            run_prospect()

        run = AIEmployeeRun.objects.get()
        self.assertIn('nothing needed doing', run.summary)

    def test_approved_work_runs_before_the_model_reads_the_funnel(self):
        """Otherwise it reads a stale picture and re-requests the same
        scrape."""
        from outreach.agent_runtime import run_prospect
        order = []

        with patch('reporting.ai.is_configured', return_value=True), \
             patch('outreach.spend.check_spend_allowed',
                   return_value=(True, '')), \
             patch('outreach.agent_tools.execute_approved',
                   side_effect=lambda: order.append('approved') or []), \
             patch('reporting.ai.claude_agent_loop',
                   side_effect=lambda **kw: order.append('loop') or {
                       'steps_used': 1, 'messages': [], 'final_text': '',
                       'stopped_reason': 'done'}):
            run_prospect()

        self.assertEqual(order, ['approved', 'loop'])

    def test_spend_is_recorded_incrementally(self):
        """A run that crashes halfway must still have counted what it
        spent, or the daily cap is fiction."""
        from outreach.agent_runtime import run_prospect

        def fake_loop(**kw):
            kw['on_usage']('claude-sonnet-5', 1000, 500)
            kw['on_usage']('claude-sonnet-5', 1000, 500)
            return {'steps_used': 2, 'messages': [], 'final_text': 'x',
                    'stopped_reason': 'done'}

        with patch('reporting.ai.is_configured', return_value=True), \
             patch('outreach.spend.check_spend_allowed',
                   return_value=(True, '')), \
             patch('outreach.spend.claude_call_cost_usd',
                   return_value=Decimal('0.01')), \
             patch('reporting.ai.claude_agent_loop', side_effect=fake_loop):
            run_prospect()

        run = AIEmployeeRun.objects.get()
        self.assertEqual(run.spend_usd, Decimal('0.02'))

    def test_missing_employee_is_reported(self):
        from outreach.agent_runtime import run_prospect
        self.employee.delete()
        self.assertIn('No AIEmployee', run_prospect())
