"""
Streaming, the live fragment, and the lookup tools.

Split from tests_agent_chat.py, which covers the thread plumbing. This
file covers what was added so the chat could show a reply arriving and
answer about specific leads rather than totals.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from admin_dashboard.models import AIEmployee, AIEmployeeRun
from outreach import agent_chat, agent_tools


def _employee():
    return AIEmployee.objects.get(slug='prospect')


class StreamingTests(TestCase):
    """The reply has to appear as it is written, not all at once."""

    def setUp(self):
        self.conv = agent_chat.start_conversation(employee=_employee())

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_streamed_text_lands_on_the_run_while_it_runs(self):
        run, _ = agent_chat.queue_turn(self.conv, 'hi')
        writer = agent_chat._PartialWriter(run)
        writer('Four ')
        writer.flush()
        run.refresh_from_db()
        self.assertEqual(run.partial_text, 'Four ')
        writer('leads.')
        writer.flush()
        run.refresh_from_db()
        self.assertEqual(run.partial_text, 'Four leads.')

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_writes_are_throttled_not_per_token(self):
        """A 400-token reply must not become 400 UPDATEs."""
        run, _ = agent_chat.queue_turn(self.conv, 'hi')
        writer = agent_chat._PartialWriter(run)
        writer._last_flush = 9e9      # far future: no interval elapses
        for _ in range(50):
            writer('x')
        run.refresh_from_db()
        self.assertEqual(run.partial_text, '')

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_partial_is_cleared_when_the_turn_finishes(self):
        """Otherwise the fragment has two sources for one reply."""
        run, _ = agent_chat.queue_turn(self.conv, 'hi')
        AIEmployeeRun.objects.filter(pk=run.pk).update(partial_text='half a re')
        with patch('reporting.ai.claude_agent_loop', return_value={
                'messages': [{'role': 'user', 'content': 'hi'},
                             {'role': 'assistant', 'content': [
                                 {'type': 'text', 'text': 'half a reply'}]}],
                'final_text': 'half a reply',
                'stopped_reason': 'done', 'steps_used': 1}):
            agent_chat.run_chat_turn(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.partial_text, '')

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_failure_also_clears_the_partial(self):
        run, _ = agent_chat.queue_turn(self.conv, 'hi')
        AIEmployeeRun.objects.filter(pk=run.pk).update(partial_text='half')
        with patch('reporting.ai.claude_agent_loop',
                   side_effect=RuntimeError('boom')):
            agent_chat.run_chat_turn(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.partial_text, '')

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_a_text_callback_is_handed_to_the_loop(self):
        run, _ = agent_chat.queue_turn(self.conv, 'hi')
        with patch('reporting.ai.claude_agent_loop', return_value={
                'messages': [], 'final_text': 'ok',
                'stopped_reason': 'done', 'steps_used': 1}) as loop:
            agent_chat.run_chat_turn(run.pk)
        self.assertTrue(callable(loop.call_args.kwargs['on_text']))


class LiveFragmentTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_superuser('z2', 'z2@e.com', 'pw')
        self.client.force_login(self.user)
        self.employee = _employee()
        self.conv = agent_chat.start_conversation(employee=self.employee)

    def _live(self):
        return self.client.get(reverse(
            'admin_dashboard:ai_chat_live_fragment',
            kwargs={'slug': self.employee.slug,
                    'conversation_id': self.conv.pk}))

    def _thread(self):
        return self.client.get(reverse(
            'admin_dashboard:ai_chat_thread_fragment',
            kwargs={'slug': self.employee.slug,
                    'conversation_id': self.conv.pk}))

    def _start_run(self, partial=''):
        return AIEmployeeRun.objects.create(
            employee=self.employee, conversation=self.conv,
            trigger='chat', status='running', partial_text=partial)

    def test_live_fragment_polls_and_shows_streamed_text(self):
        self._start_run(partial='Four leads so far')
        resp = self._live()
        self.assertContains(resp, 'hx-trigger')
        self.assertContains(resp, 'Four leads so far')

    def test_dots_only_before_the_first_token(self):
        """No 'thinking' label — just the animated dots."""
        self._start_run()
        resp = self._live()
        self.assertContains(resp, 'chat-dots')
        self.assertNotContains(resp, 'thinking')

    def test_finished_poll_asks_once_for_the_settled_thread(self):
        resp = self._live()
        self.assertContains(resp, 'hx-trigger="load"')
        self.assertContains(resp, '/thread/')

    def test_idle_thread_render_is_completely_inert(self):
        """The regression this guards: the finished branch reloads the
        whole thread, and the whole thread includes this fragment. If it
        rendered the reload branch inside a thread render too, the two
        would fetch each other forever."""
        resp = self._thread()
        self.assertContains(resp, 'id="chat-live"')
        self.assertNotContains(resp, 'hx-trigger')
        self.assertNotContains(resp, 'hx-get')

    def test_streaming_thread_render_still_polls(self):
        self._start_run(partial='partial answer')
        resp = self._thread()
        self.assertContains(resp, 'hx-trigger')
        self.assertContains(resp, 'partial answer')


class LookupToolTests(TestCase):
    """The tools that answer about specifics rather than totals."""

    def setUp(self):
        from outreach.models import Lead
        self.a = Lead.objects.create(
            firm_name='Toler Law Group', attorney_name='Jeff Toler',
            email='jtoler@tlgiplaw.com', city='Austin', state='Texas',
            business_type='Law Firm', source='apify',
            icebreaker='A true measured line.', score=6)
        self.b = Lead.objects.create(
            firm_name='Hill Law Firm', attorney_name='Justin Hill',
            email='justin@jahlawfirm.com', city='San Antonio',
            state='Texas', business_type='Law Firm', source='apify')

    def _call(self, name, arg=None):
        return agent_tools._resolve(name)(arg or {})

    def test_find_leads_returns_named_rows_not_counts(self):
        out = self._call('find_leads')
        self.assertEqual(out['matched'], 2)
        self.assertEqual({r['firm'] for r in out['leads']},
                         {'Toler Law Group', 'Hill Law Firm'})

    def test_find_leads_filters(self):
        self.assertEqual(self._call('find_leads', {'query': 'Toler'})
                         ['matched'], 1)
        self.assertEqual(self._call('find_leads', {'has_icebreaker': True})
                         ['matched'], 1)
        self.assertEqual(self._call('find_leads', {'has_icebreaker': False})
                         ['matched'], 1)
        self.assertEqual(self._call('find_leads', {'city': 'Austin'})
                         ['matched'], 1)

    def test_truncation_is_reported_not_silent(self):
        """A tool that quietly returns the first 25 of 900 lets the model
        say "we have 25 leads" with total confidence."""
        out = self._call('find_leads', {'limit': 1})
        self.assertTrue(out['truncated'])
        self.assertEqual(out['returned'], 1)
        self.assertEqual(out['matched'], 2)

    def test_lead_detail_gives_the_copy_and_the_facts_behind_it(self):
        """Judging whether a line is honest is impossible without the
        numbers it claims to describe, so both travel together."""
        out = self._call('lead_detail', {'lead': 'Toler'})
        self.assertEqual(out['icebreaker'], 'A true measured line.')
        self.assertIn('pagespeed_performance', out['measured'])

    def test_lead_detail_resolves_by_id_and_email(self):
        self.assertEqual(
            self._call('lead_detail', {'lead': str(self.a.pk)})['firm'],
            'Toler Law Group')
        self.assertEqual(
            self._call('lead_detail',
                       {'lead': 'justin@jahlawfirm.com'})['firm'],
            'Hill Law Firm')

    def test_lead_detail_says_so_when_ambiguous(self):
        out = self._call('lead_detail', {'lead': 'Law'})
        self.assertTrue(out['ambiguous'])
        self.assertEqual(len(out['candidates']), 2)

    def test_lead_detail_missing_is_an_answer_not_a_crash(self):
        self.assertIn('No lead matches',
                      self._call('lead_detail', {'lead': 'Nobody Ltd'}))

    def test_limits_are_clamped(self):
        self.assertEqual(agent_tools._clamp(9999, 25, 100), 100)
        self.assertEqual(agent_tools._clamp(0, 25, 100), 1)
        self.assertEqual(agent_tools._clamp(None, 25, 100), 25)
        self.assertEqual(agent_tools._clamp('nonsense', 25, 100), 25)

    def test_every_lookup_tool_runs_and_is_read_class(self):
        for name in ('find_leads', 'list_campaigns', 'list_offers',
                     'recent_replies', 'sourcing_history', 'spend_summary'):
            self.assertEqual(
                agent_tools.tool_kind(name), agent_tools.READ,
                f'{name} must not be able to write or spend')
            self._call(name)

    def test_lookup_tools_are_offered_in_chat(self):
        names = {t['name'] for t in agent_tools.anthropic_tools(
            exclude=agent_chat.CHAT_WITHHELD_TOOLS)}
        self.assertIn('find_leads', names)
        self.assertIn('lead_detail', names)
        self.assertNotIn('write_journal', names)

    def test_every_tool_has_an_implementation(self):
        """A tool advertised to the model with no impl behind it is a
        guaranteed runtime failure mid-conversation."""
        for tool in agent_tools.TOOLS:
            name = tool['name']
            if tool['kind'] == agent_tools.COMMIT:
                continue          # filed for approval, never dispatched
            self.assertIsNotNone(
                agent_tools._resolve(name) if name != 'write_journal' else True,
                f'{name} has no implementation')
