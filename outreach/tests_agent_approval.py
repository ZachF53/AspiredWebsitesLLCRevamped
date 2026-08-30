"""
Approving a COMMIT call inside the conversation, and the progress lines
a long tool emits while it works.

The money rules are the point. Approval now EXECUTES rather than parking
the work until the next scheduled wake-up, so the tests that matter are
the ones proving the model still cannot spend on its own, an approval
cannot be replayed, and a rejection is recorded where the model will see
it.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from admin_dashboard.models import (
    AIEmployee,
    AIEmployeeAction,
    AIEmployeeRun,
)
from outreach import agent_chat, agent_tools


def _employee():
    return AIEmployee.objects.get(slug='prospect')


class NicheToBusinessTypeTests(TestCase):
    """A search niche is not a business type.

    "family law" ran as business_type "Family Law", which no Apollo
    industry normalises to — so the ICP screen rejected every row the
    scrape had just paid for, and any survivor was blocked again by the
    campaign segment gate.
    """

    def test_law_niches_all_resolve_to_law_firm(self):
        from outreach.apify_source import business_type_for_niche as f
        for niche in ('family law', 'personal injury law', 'law firm',
                      'estate planning attorney', 'criminal defense lawyer'):
            self.assertEqual(f(niche), 'Law Firm', niche)

    def test_other_niches_resolve_sensibly(self):
        from outreach.apify_source import business_type_for_niche as f
        self.assertEqual(f('dental practice'), 'Dentist')
        self.assertEqual(f('orthodontist'), 'Dentist')
        self.assertEqual(f('cpa'), 'Accounting')
        self.assertEqual(f('medical clinic'), 'Medical Practice')

    def test_unknown_niche_does_not_constrain(self):
        """Importing leads that need a segment correction beats
        importing none."""
        from outreach.apify_source import business_type_for_niche as f
        self.assertEqual(f('yoga studio'), '')

    def test_a_family_law_row_now_survives_the_screen(self):
        from outreach.apify_source import (
            business_type_for_niche, screen_contact,
        )
        row = {'company_name': 'Smith Family Law', 'job_title': 'Owner',
               'company_size': 8, 'industry': 'Law Practice',
               'company_state': 'Texas'}
        # The old behaviour, kept as the thing being guarded against.
        self.assertIn('targeting Family Law', screen_contact(
            row, target_state='TX', target_business_type='Family Law'))
        # The fix.
        self.assertEqual(screen_contact(
            row, target_state='TX',
            target_business_type=business_type_for_niche('family law')), '')


class ProgressReportingTests(TestCase):

    def setUp(self):
        self.run = AIEmployeeRun.objects.create(
            employee=_employee(), trigger='chat')
        self.action = AIEmployeeAction.objects.create(
            run=self.run, tool_name='start_scrape', tool_input={})

    def test_lines_are_written_as_they_happen(self):
        """Buffered lines would all appear at the end, which is the
        spinner this replaces."""
        report = agent_tools.action_reporter(self.action)
        report('100 returned by Apify')
        self.action.refresh_from_db()
        self.assertEqual(self.action.progress, ['100 returned by Apify'])
        report('18 rejected by the ICP screen')
        self.action.refresh_from_db()
        self.assertEqual(len(self.action.progress), 2)

    def test_blank_lines_are_ignored(self):
        report = agent_tools.action_reporter(self.action)
        report('')
        report('   ')
        self.action.refresh_from_db()
        self.assertEqual(self.action.progress, [])

    def test_a_runaway_tool_cannot_grow_the_row_without_bound(self):
        report = agent_tools.action_reporter(self.action)
        for i in range(120):
            report(f'line {i}')
        self.action.refresh_from_db()
        self.assertEqual(len(self.action.progress), 40)
        self.assertEqual(self.action.progress[-1], 'line 119')

    def test_every_tool_accepts_a_reporter(self):
        """The executor calls all of them uniformly, so a signature that
        does not take one is a crash mid-conversation."""
        for tool in agent_tools.TOOLS:
            impl = agent_tools._resolve(tool['name'])
            if impl is None:
                continue
            import inspect
            params = list(inspect.signature(impl).parameters)
            self.assertGreaterEqual(
                len(params), 2,
                f"{tool['name']} does not accept a progress reporter")

    def test_progress_attaches_to_the_right_tool_in_history(self):
        messages = [
            {'role': 'user', 'content': 'scrape san antonio'},
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 't1',
                 'name': 'start_scrape', 'input': {}}]},
        ]
        self.action.progress = ['100 returned', '82 imported']
        self.action.save(update_fields=['progress'])
        rendered = agent_chat.render_transcript(
            messages, actions=[self.action])
        tool_item = [r for r in rendered if r['kind'] == 'tool'][0]
        self.assertEqual(tool_item['progress'], ['100 returned',
                                                 '82 imported'])

    def test_mismatched_action_does_not_hang_lines_on_the_wrong_tool(self):
        """Showing "82 imported" under funnel_status is worse than
        showing no progress at all."""
        messages = [
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 't1',
                 'name': 'funnel_status', 'input': {}}]},
        ]
        self.action.progress = ['82 imported']
        self.action.save(update_fields=['progress'])
        rendered = agent_chat.render_transcript(
            messages, actions=[self.action])
        tool_item = [r for r in rendered if r['kind'] == 'tool'][0]
        self.assertEqual(tool_item['progress'], [])


class ApprovedExecutionTests(TestCase):

    def setUp(self):
        self.conv = agent_chat.start_conversation(employee=_employee())
        self.run = AIEmployeeRun.objects.create(
            employee=_employee(), conversation=self.conv, trigger='chat')

    def _pending(self, tool='start_scrape'):
        return AIEmployeeAction.objects.create(
            run=self.run, tool_name=tool,
            tool_input={'city': 'San Antonio', 'state': 'TX',
                        'niche': 'family law', 'reason': 'nothing left'},
            requires_approval=True,
            result='Awaiting human approval — not executed.')

    def test_a_commit_call_does_not_run_when_the_model_asks(self):
        """The load-bearing rule. Filing must not execute."""
        executor = agent_tools.make_executor(self.run)
        with patch.object(agent_tools, '_start_scrape') as impl:
            out = executor('start_scrape', {'city': 'Austin', 'state': 'TX',
                                            'reason': 'x'})
        impl.assert_not_called()
        self.assertFalse(out['executed'])
        self.assertEqual(out['status'], 'awaiting_approval')

    def test_approval_executes_and_reports_into_the_thread(self):
        action = self._pending()
        action.approved = True
        action.save(update_fields=['approved'])
        with patch.object(agent_tools, '_start_scrape',
                          return_value={'scraped': True, 'imported': 82}):
            self.assertEqual(
                agent_chat.run_approved_action(action.pk), 'ok')
        action.refresh_from_db()
        self.conv.refresh_from_db()
        self.assertIsNotNone(action.executed_at)
        self.assertIn('82', str(self.conv.messages[-1]['content']))
        self.assertIn('has now RUN', str(self.conv.messages[-1]['content']))

    def test_an_unapproved_action_is_refused(self):
        action = self._pending()
        with patch.object(agent_tools, '_start_scrape') as impl:
            self.assertEqual(
                agent_chat.run_approved_action(action.pk), 'not approved')
        impl.assert_not_called()

    def test_execution_cannot_be_replayed(self):
        """Approval is permanent, so without the executed_at guard a
        retried task charges the card twice."""
        action = self._pending()
        action.approved = True
        action.save(update_fields=['approved'])
        with patch.object(agent_tools, '_start_scrape',
                          return_value={'scraped': True}) as impl:
            agent_chat.run_approved_action(action.pk)
            self.assertEqual(agent_chat.run_approved_action(action.pk),
                             'already executed')
        self.assertEqual(impl.call_count, 1)

    def test_a_failing_tool_is_recorded_not_swallowed(self):
        action = self._pending()
        action.approved = True
        action.save(update_fields=['approved'])
        with patch.object(agent_tools, '_start_scrape',
                          side_effect=RuntimeError('apify down')):
            self.assertEqual(
                agent_chat.run_approved_action(action.pk), 'failed')
        action.refresh_from_db()
        self.assertIn('apify down', action.result)
        self.assertIsNotNone(action.executed_at)


class ChatApprovalViewTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_superuser('z3', 'z3@e.com', 'pw')
        self.client.force_login(self.user)
        self.employee = _employee()
        self.conv = agent_chat.start_conversation(employee=self.employee)
        self.run = AIEmployeeRun.objects.create(
            employee=self.employee, conversation=self.conv, trigger='chat')
        self.action = AIEmployeeAction.objects.create(
            run=self.run, tool_name='start_scrape',
            tool_input={'city': 'San Antonio', 'state': 'TX',
                        'reason': 'Austin is exhausted'},
            requires_approval=True)

    def _url(self):
        return reverse('admin_dashboard:ai_chat_decide', kwargs={
            'slug': self.employee.slug, 'conversation_id': self.conv.pk,
            'action_id': self.action.pk})

    def test_pending_approval_is_shown_in_the_thread(self):
        resp = self.client.get(reverse(
            'admin_dashboard:ai_chat_thread_fragment',
            kwargs={'slug': self.employee.slug,
                    'conversation_id': self.conv.pk}))
        self.assertContains(resp, 'Needs your approval')
        self.assertContains(resp, 'Austin is exhausted')
        self.assertContains(resp, 'Approve')

    def test_approving_queues_the_work(self):
        with patch('outreach.tasks.run_approved_action_task.delay') as delay:
            resp = self.client.post(self._url(), {'decision': 'approve'})
        self.assertEqual(resp.status_code, 200)
        delay.assert_called_once_with(self.action.pk)
        self.action.refresh_from_db()
        self.assertTrue(self.action.approved)

    def test_rejecting_tells_the_model_so(self):
        """Otherwise it asks for the same thing again next turn."""
        with patch('outreach.tasks.run_approved_action_task.delay') as delay:
            self.client.post(self._url(), {'decision': 'reject'})
        delay.assert_not_called()
        self.action.refresh_from_db()
        self.conv.refresh_from_db()
        self.assertFalse(self.action.approved)
        self.assertIn('REJECTED', str(self.conv.messages[-1]['content']))

    def test_broker_failure_hands_the_approval_back(self):
        """A row marked approved that nothing will execute is worse than
        an un-approved one — it looks done."""
        with patch('outreach.tasks.run_approved_action_task.delay',
                   side_effect=OSError('redis down')):
            resp = self.client.post(self._url(), {'decision': 'approve'})
        self.action.refresh_from_db()
        self.assertIsNone(self.action.approved)
        self.assertContains(resp, 'broker refused')

    def test_an_already_decided_action_cannot_be_decided_again(self):
        self.action.approved = True
        self.action.save(update_fields=['approved'])
        resp = self.client.post(self._url(), {'decision': 'approve'})
        self.assertEqual(resp.status_code, 404)

    def test_approval_requires_admin(self):
        self.client.logout()
        with patch('outreach.tasks.run_approved_action_task.delay') as delay:
            resp = self.client.post(self._url(), {'decision': 'approve'})
        self.assertNotEqual(resp.status_code, 200)
        delay.assert_not_called()


class CreateCampaignToolTests(TestCase):

    def setUp(self):
        from outreach.models import Offer
        self.offer = Offer.objects.filter(key='security_review').first()
        if self.offer is None:
            self.offer = Offer.objects.create(
                key='security_review', name='Free security review',
                pitch='x', restate='y', ask='z')

    def test_it_is_commit_class(self):
        """Creating a campaign in Instantly is a step toward mail
        reaching strangers, so it waits for a human."""
        self.assertEqual(agent_tools.tool_kind('create_campaign'),
                         agent_tools.COMMIT)

    def test_campaign_is_created_paused_and_inactive(self):
        from outreach.models import OutreachCampaign
        with patch('outreach.instantly.create_campaign',
                   return_value={'id': 'camp_123'}):
            out = agent_tools._create_campaign({
                'name': 'TX Family Law [security review]',
                'niche': 'family law', 'state': 'TX',
                'offer': 'security_review', 'reason': 'no arm exists'})
        self.assertTrue(out['created'])
        campaign = OutreachCampaign.objects.get(slug=out['slug'])
        self.assertFalse(campaign.active)
        self.assertEqual(campaign.instantly_campaign_id, 'camp_123')
        # The niche is the search phrase; the segment is what the gate
        # compares against.
        self.assertEqual(campaign.business_type, 'Law Firm')

    def test_copy_that_fails_pre_flight_creates_nothing(self):
        from outreach.models import OutreachCampaign
        with patch('outreach.sequences.describe_problems',
                   return_value=['no postal address']), \
                patch('outreach.instantly.create_campaign') as create:
            out = agent_tools._create_campaign({
                'name': 'Bad Copy Arm', 'niche': 'law firm',
                'state': 'TX', 'reason': 'x'})
        create.assert_not_called()
        self.assertFalse(out['created'])
        self.assertEqual(OutreachCampaign.objects.count(), 0)

    def test_duplicate_slug_is_refused(self):
        from outreach.models import OutreachCampaign
        OutreachCampaign.objects.create(
            name='TX Law Arm', slug='tx-law-arm', niche='law firm',
            state='TX', business_type='Law Firm')
        with patch('outreach.instantly.create_campaign') as create:
            out = agent_tools._create_campaign({
                'name': 'TX Law Arm', 'niche': 'law firm',
                'state': 'TX', 'reason': 'x'})
        create.assert_not_called()
        self.assertFalse(out['created'])
        self.assertIn('already exists', out['reason'])


class SequenceToolTests(TestCase):
    """Reading and writing the copy a campaign sends."""

    def setUp(self):
        from outreach.models import Offer, OutreachCampaign
        # Offers are seeded by a management command, not a migration, so
        # the test database has none. Build the two this class needs.
        self.offer, _ = Offer.objects.get_or_create(
            key='security_review',
            defaults={'name': 'Free security + performance review',
                      'pitch': 'p', 'restate': 'r', 'ask': 'a'})
        self.other_offer, _ = Offer.objects.get_or_create(
            key='speed_guarantee',
            defaults={'name': 'Speed fix, guaranteed or free',
                      'pitch': 'p', 'restate': 'r', 'ask': 'a'})
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law [security review]', slug='tx-law-security',
            niche='law firm', state='TX', business_type='Law Firm',
            offer=self.offer, instantly_campaign_id='camp_1', active=False)

    def test_preview_is_free_and_writes_nothing(self):
        self.assertEqual(agent_tools.tool_kind('preview_sequence'),
                         agent_tools.READ)
        with patch('outreach.instantly.update_campaign_sequence') as write:
            out = agent_tools._preview_sequence({'offer': 'security_review'})
        write.assert_not_called()
        self.assertTrue(out['previewed'])
        self.assertEqual(out['touches'], 4)
        self.assertTrue(out['would_pass_preflight'])
        self.assertIn('Subject', str(out['steps'][0]).replace('subject',
                                                              'Subject'))

    def test_preview_shows_the_body_not_a_summary(self):
        """He has to be able to read what strangers receive."""
        out = agent_tools._preview_sequence({'campaign': 'tx-law-security'})
        first = out['steps'][0]
        self.assertGreater(len(first['body']), 200)
        self.assertGreater(first['words'], 50)

    def test_writing_a_sequence_needs_approval(self):
        self.assertEqual(agent_tools.tool_kind('set_campaign_sequence'),
                         agent_tools.COMMIT)

    def test_sequence_is_written_to_the_named_campaign(self):
        with patch('outreach.instantly.update_campaign_sequence') as write:
            out = agent_tools._set_campaign_sequence({
                'campaign': 'tx-law-security', 'offer': 'security_review',
                'reason': 'switching the angle'})
        self.assertTrue(out['updated'])
        write.assert_called_once()
        campaign_id, steps = write.call_args.args
        self.assertEqual(campaign_id, 'camp_1')
        self.assertEqual(len(steps), 4)

    def test_custom_copy_is_accepted_when_it_passes_preflight(self):
        body = ('Hi {{firstName}},\n\n{{icebreaker}}\n\nI build websites '
                'for law firms.\n\nZachery Long\nAspired Websites LLC\n'
                '123 Main St, Austin TX\nUnsubscribe: {{unsubscribe}}')
        with patch('outreach.sequences.describe_problems', return_value=[]), \
                patch('outreach.instantly.update_campaign_sequence') as write:
            out = agent_tools._set_campaign_sequence({
                'campaign': 'tx-law-security',
                'steps': [{'subject': 'quick question', 'body': body}],
                'reason': 'bespoke wording for this arm'})
        self.assertTrue(out['updated'])
        self.assertEqual(out['source'], 'custom copy')
        _, steps = write.call_args.args
        self.assertEqual(len(steps), 1)

    def test_copy_that_fails_preflight_never_reaches_instantly(self):
        """Instantly is where copy becomes email. A campaign whose copy
        fails pre-flight is a campaign somebody eventually starts."""
        with patch('outreach.sequences.describe_problems',
                   return_value=['Step 1: missing the CAN-SPAM footer.']), \
                patch('outreach.instantly.update_campaign_sequence') as write:
            out = agent_tools._set_campaign_sequence({
                'campaign': 'tx-law-security',
                'steps': [{'subject': 's', 'body': 'no footer here'}],
                'reason': 'x'})
        write.assert_not_called()
        self.assertFalse(out['updated'])
        self.assertIn('CAN-SPAM', out['problems'][0])

    def test_campaign_without_an_instantly_id_is_refused(self):
        from outreach.models import OutreachCampaign
        OutreachCampaign.objects.create(
            name='Django only', slug='django-only', niche='law firm',
            state='TX', business_type='Law Firm')
        with patch('outreach.instantly.update_campaign_sequence') as write:
            out = agent_tools._set_campaign_sequence({
                'campaign': 'django-only', 'reason': 'x'})
        write.assert_not_called()
        self.assertFalse(out['updated'])
        self.assertIn('no Instantly campaign id', out['reason'])

    def test_unknown_campaign_is_an_answer_not_a_crash(self):
        out = agent_tools._set_campaign_sequence({
            'campaign': 'nope', 'reason': 'x'})
        self.assertFalse(out['updated'])
        self.assertIn('No campaign matches', out['reason'])

    def test_writing_a_sequence_never_activates_the_campaign(self):
        with patch('outreach.instantly.update_campaign_sequence'):
            agent_tools._set_campaign_sequence({
                'campaign': 'tx-law-security', 'reason': 'x'})
        self.campaign.refresh_from_db()
        self.assertFalse(self.campaign.active)

    def test_changing_the_offer_updates_the_django_arm_too(self):
        with patch('outreach.instantly.update_campaign_sequence'):
            agent_tools._set_campaign_sequence({
                'campaign': 'tx-law-security', 'offer': self.other_offer.key,
                'reason': 'testing a different angle'})
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.offer_id, self.other_offer.pk)

    def test_both_sequence_tools_are_offered_in_chat(self):
        from outreach import agent_chat
        names = {t['name'] for t in agent_tools.anthropic_tools(
            exclude=agent_chat.CHAT_WITHHELD_TOOLS)}
        self.assertIn('preview_sequence', names)
        self.assertIn('set_campaign_sequence', names)
        self.assertIn('create_campaign', names)
        self.assertIn('campaign_stats', names)


class CampaignStatsTests(TestCase):
    """"What campaigns do I have and how are they doing" — local arms
    merged with live Instantly counters."""

    def setUp(self):
        from outreach.models import Offer, OutreachCampaign
        offer, _ = Offer.objects.get_or_create(
            key='security_review',
            defaults={'name': 'Free security review', 'pitch': 'p',
                      'restate': 'r', 'ask': 'a'})
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law [security review]', slug='tx-law-sec',
            niche='law firm', state='TX', business_type='Law Firm',
            offer=offer, instantly_campaign_id='camp_1', active=True,
            leads_pushed=400)

    def test_it_is_read_class(self):
        self.assertEqual(agent_tools.tool_kind('campaign_stats'),
                         agent_tools.READ)

    def test_live_counters_are_merged_onto_the_local_arm(self):
        with patch('outreach.instantly.campaign_analytics', return_value=[
                {'campaign_id': 'camp_1', 'emails_sent_count': 400,
                 'open_count': 160, 'reply_count': 12,
                 'bounced_count': 4}]):
            out = agent_tools._campaign_stats({})
        row = out['campaigns'][0]
        self.assertEqual(row['emails_sent'], 400)
        self.assertEqual(row['replies'], 12)
        self.assertEqual(row['reply_rate_pct'], 3.0)
        self.assertTrue(row['pushable'])

    def test_a_thin_arm_is_labelled_as_noise(self):
        """Ranking arms on 20 sends is how you learn the wrong lesson
        and then scale it."""
        with patch('outreach.instantly.campaign_analytics', return_value=[
                {'campaign_id': 'camp_1', 'emails_sent_count': 20,
                 'reply_count': 2}]):
            out = agent_tools._campaign_stats({})
        row = out['campaigns'][0]
        self.assertFalse(row['enough_data_to_judge'])
        self.assertIn('noise', row['caveat'])

    def test_a_dangerous_bounce_rate_is_flagged(self):
        """Above 3% Google and Microsoft start filtering the domain."""
        with patch('outreach.instantly.campaign_analytics', return_value=[
                {'campaign_id': 'camp_1', 'emails_sent_count': 400,
                 'bounced_count': 20}]):
            out = agent_tools._campaign_stats({})
        self.assertTrue(out['campaigns'][0]['bounce_rate_is_dangerous'])

    def test_renamed_instantly_fields_still_read(self):
        """A stats tool that reports 0 replies because a key moved is
        worse than one that reports nothing."""
        with patch('outreach.instantly.campaign_analytics', return_value=[
                {'id': 'camp_1', 'sent_count': 100,
                 'replies_count': 5}]):
            out = agent_tools._campaign_stats({})
        row = out['campaigns'][0]
        self.assertEqual(row['emails_sent'], 100)
        self.assertEqual(row['replies'], 5)

    def test_instantly_being_down_is_said_out_loud(self):
        """Zeroes that mean "unreachable" must not read as "nothing was
        sent"."""
        with patch('outreach.instantly.campaign_analytics',
                   side_effect=RuntimeError('connection refused')):
            out = agent_tools._campaign_stats({})
        self.assertIn('instantly_unreachable', out)
        self.assertIn('NOT because nothing was sent', out['note'])
        # The local half still arrives.
        self.assertEqual(out['campaigns'][0]['leads_pushed'], 400)

    def test_can_be_limited_to_one_arm(self):
        from outreach.models import OutreachCampaign
        OutreachCampaign.objects.create(
            name='GA Law', slug='ga-law', niche='law firm', state='GA',
            business_type='Law Firm')
        with patch('outreach.instantly.campaign_analytics', return_value=[]):
            out = agent_tools._campaign_stats({'campaign': 'ga-law'})
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['campaigns'][0]['slug'], 'ga-law')
