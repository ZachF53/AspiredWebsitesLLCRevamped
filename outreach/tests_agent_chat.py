"""
Tests for the Prospect chat pane (COLD_OUTREACH_AGENT.md §5.1).

Nothing here calls the real Claude API — the agent loop is patched. What
is under test is the plumbing around it: that a thread accumulates in the
shape the API will accept on replay, that the guards a scheduled run has
still apply to a chat turn, and that the transcript renderer never shows
a tool_result as something a human said.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from admin_dashboard.models import (
    AIEmployee,
    AIEmployeeConversation,
    AIEmployeeRun,
)
from outreach import agent_chat


def _employee():
    return AIEmployee.objects.get(slug='prospect')


class TranscriptRenderTests(TestCase):

    def test_tool_results_are_not_rendered_as_user_messages(self):
        """A user turn carrying a LIST is the tool_result answering the
        assistant, not something Zachery typed. Rendering it as a user
        bubble would put raw JSON in the chat under his name."""
        messages = [
            {'role': 'user', 'content': 'how many leads?'},
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 't1',
                 'name': 'funnel_status', 'input': {}},
            ]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 't1',
                 'content': '{"leads": 4}'},
            ]},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'Four leads.'},
            ]},
        ]
        rendered = agent_chat.render_transcript(messages)
        kinds = [r['kind'] for r in rendered]
        self.assertEqual(kinds, ['user', 'tool', 'assistant'])
        self.assertEqual(rendered[0]['text'], 'how many leads?')
        self.assertEqual(rendered[1]['tool_name'], 'funnel_status')
        self.assertEqual(rendered[2]['text'], 'Four leads.')

    def test_thinking_blocks_are_stored_but_not_shown(self):
        messages = [{'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'hmm', 'signature': 'sig'},
            {'type': 'text', 'text': 'Answer.'},
        ]}]
        rendered = agent_chat.render_transcript(messages)
        self.assertEqual([r['kind'] for r in rendered], ['assistant'])

    def test_empty_thread_renders_nothing(self):
        self.assertEqual(agent_chat.render_transcript([]), [])
        self.assertEqual(agent_chat.render_transcript(None), [])


class QueueTurnTests(TestCase):

    def setUp(self):
        self.conv = agent_chat.start_conversation(employee=_employee())

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_message_is_stored_before_any_model_call(self):
        """You must see what you typed immediately, whether or not a
        worker ever picks the turn up."""
        run, err = agent_chat.queue_turn(self.conv, 'what is blocked?')
        self.assertEqual(err, '')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.messages[-1],
                         {'role': 'user', 'content': 'what is blocked?'})
        self.assertEqual(run.trigger, 'chat')
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.conversation_id, self.conv.pk)

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_first_message_titles_the_thread(self):
        agent_chat.queue_turn(self.conv, 'How is Houston going?')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.title, 'How is Houston going?')
        agent_chat.queue_turn(self.conv, 'and Dallas?')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.title, 'How is Houston going?')

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_long_title_is_truncated(self):
        agent_chat.queue_turn(self.conv, 'x' * 300)
        self.conv.refresh_from_db()
        self.assertLessEqual(len(self.conv.title), agent_chat.TITLE_MAX)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_missing_api_key_refuses_without_creating_a_run(self):
        """A run row that never ran makes the log lie."""
        run, err = agent_chat.queue_turn(self.conv, 'hello')
        self.assertIsNone(run)
        self.assertIn('ANTHROPIC_API_KEY', err)
        self.assertEqual(AIEmployeeRun.objects.count(), 0)

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_empty_message_is_refused(self):
        run, err = agent_chat.queue_turn(self.conv, '   ')
        self.assertIsNone(run)
        self.assertEqual(AIEmployeeRun.objects.count(), 0)

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_daily_spend_cap_refuses_a_turn(self):
        """The chat inherits the scheduled run's dollar cap. Being asked
        in a chat box is not a reason to exceed today's budget."""
        with patch('outreach.spend.check_spend_allowed',
                   return_value=(False, 'Daily AI spend cap reached.')):
            run, err = agent_chat.queue_turn(self.conv, 'hello')
        self.assertIsNone(run)
        self.assertIn('cap', err.lower())


class RunChatTurnTests(TestCase):

    def setUp(self):
        self.conv = agent_chat.start_conversation(employee=_employee())

    @override_settings(ANTHROPIC_API_KEY='k')
    def _turn(self, text, loop_return):
        run, err = agent_chat.queue_turn(self.conv, text)
        self.assertEqual(err, '')
        with patch('reporting.ai.claude_agent_loop',
                   return_value=loop_return) as loop:
            agent_chat.run_chat_turn(run.pk)
        run.refresh_from_db()
        self.conv.refresh_from_db()
        return run, loop

    def test_answer_is_stored_and_run_completes(self):
        run, _ = self._turn('hi', {
            'messages': [
                {'role': 'user', 'content': 'hi'},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'Four leads.'}]},
            ],
            'final_text': 'Four leads.',
            'stopped_reason': 'done',
            'steps_used': 1,
        })
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.summary, 'Four leads.')
        rendered = agent_chat.render_transcript(self.conv.messages)
        self.assertEqual(rendered[-1]['text'], 'Four leads.')

    def test_prior_turns_are_passed_back_to_the_model(self):
        """The whole point of the thread: turn two must see turn one, in
        wire shape rather than as a prose summary."""
        self._turn('first', {
            'messages': [
                {'role': 'user', 'content': 'first'},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'ok'}]},
            ],
            'final_text': 'ok', 'stopped_reason': 'done', 'steps_used': 1,
        })
        _, loop = self._turn('second', {
            'messages': [], 'final_text': 'ok2',
            'stopped_reason': 'done', 'steps_used': 1,
        })
        kwargs = loop.call_args.kwargs
        self.assertEqual(kwargs['user_message'], 'second')
        # Prior turns exclude the message being answered — passing both
        # would ask the question twice.
        self.assertEqual(len(kwargs['prior_messages']), 2)
        self.assertNotIn(
            'second', str(kwargs['prior_messages']))

    def test_write_journal_is_withheld_from_chat(self):
        """A passing question must not be able to overwrite the memory
        the last scheduled run wrote."""
        _, loop = self._turn('hi', {
            'messages': [], 'final_text': 'ok',
            'stopped_reason': 'done', 'steps_used': 1,
        })
        names = [t['name'] for t in loop.call_args.kwargs['tools']]
        self.assertNotIn('write_journal', names)
        self.assertIn('funnel_status', names)

    def test_max_steps_without_an_answer_still_says_something(self):
        run, _ = self._turn('hi', {
            'messages': [{'role': 'user', 'content': 'hi'}],
            'final_text': '', 'stopped_reason': 'max_steps', 'steps_used': 6,
        })
        self.assertEqual(run.status, 'completed')
        rendered = agent_chat.render_transcript(self.conv.messages)
        self.assertEqual(rendered[-1]['kind'], 'assistant')
        self.assertIn('tool calls', rendered[-1]['text'])

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_loop_exception_fails_the_run_visibly(self):
        """A chat that silently stops is indistinguishable from one still
        thinking, so the failure has to land on the run."""
        run, _err = agent_chat.queue_turn(self.conv, 'hi')
        with patch('reporting.ai.claude_agent_loop',
                   side_effect=RuntimeError('boom')):
            agent_chat.run_chat_turn(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('boom', run.summary)

    def test_run_without_a_conversation_is_refused(self):
        run = AIEmployeeRun.objects.create(
            employee=_employee(), trigger='scheduled')
        self.assertIn('not part of a conversation',
                      agent_chat.run_chat_turn(run.pk))


class ChatViewTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_superuser(
            'zach', 'z@example.com', 'pw')
        self.client.force_login(self.user)
        self.employee = _employee()

    def _url(self, name, **kw):
        return reverse(f'admin_dashboard:{name}',
                       kwargs={'slug': self.employee.slug, **kw})

    def test_chat_page_renders_with_no_conversations(self):
        resp = self.client.get(self._url('ai_employee_chat'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'New conversation')

    def test_new_conversation_redirects_into_the_thread(self):
        resp = self.client.post(self._url('ai_chat_new'))
        self.assertEqual(resp.status_code, 302)
        conv = AIEmployeeConversation.objects.get()
        self.assertIn(str(conv.pk), resp.url)

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_send_queues_a_turn_and_echoes_the_message(self):
        conv = agent_chat.start_conversation(employee=self.employee)
        with patch('outreach.tasks.run_chat_turn_task.delay') as delay:
            resp = self.client.post(
                self._url('ai_chat_send', conversation_id=conv.pk),
                {'message': 'how many leads?'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'how many leads?')
        delay.assert_called_once()

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_broker_failure_fails_the_run_rather_than_hanging(self):
        """Otherwise the fragment polls a run that will never finish."""
        conv = agent_chat.start_conversation(employee=self.employee)
        with patch('outreach.tasks.run_chat_turn_task.delay',
                   side_effect=OSError('redis down')):
            self.client.post(
                self._url('ai_chat_send', conversation_id=conv.pk),
                {'message': 'hi'})
        run = AIEmployeeRun.objects.get()
        self.assertEqual(run.status, 'failed')
        self.assertIn('broker', run.summary)

    @override_settings(ANTHROPIC_API_KEY='k')
    def test_second_send_is_refused_while_one_is_in_flight(self):
        """Two turns at once would race on conversation.messages and the
        loser's answer would vanish."""
        conv = agent_chat.start_conversation(employee=self.employee)
        with patch('outreach.tasks.run_chat_turn_task.delay'):
            self.client.post(
                self._url('ai_chat_send', conversation_id=conv.pk),
                {'message': 'first'})
            resp = self.client.post(
                self._url('ai_chat_send', conversation_id=conv.pk),
                {'message': 'second'})
        self.assertContains(resp, 'still answering')
        self.assertEqual(AIEmployeeRun.objects.count(), 1)

    def test_thread_fragment_polls_only_while_running(self):
        conv = agent_chat.start_conversation(employee=self.employee)
        url = self._url('ai_chat_thread_fragment', conversation_id=conv.pk)

        resp = self.client.get(url)
        self.assertNotContains(resp, 'hx-trigger')

        AIEmployeeRun.objects.create(
            employee=self.employee, conversation=conv,
            trigger='chat', status='running')
        resp = self.client.get(url)
        self.assertContains(resp, 'hx-trigger')

    def test_archive_hides_the_thread_but_keeps_it(self):
        conv = agent_chat.start_conversation(employee=self.employee)
        self.client.post(
            self._url('ai_chat_archive', conversation_id=conv.pk))
        conv.refresh_from_db()
        self.assertTrue(conv.archived)
        # Gone from the sidebar but still in the database, so the runs,
        # actions and spend attached to it stay auditable.
        resp = self.client.get(self._url('ai_employee_chat'))
        self.assertContains(resp, 'No conversations yet')
        self.assertEqual(AIEmployeeConversation.objects.count(), 1)

    def test_chat_requires_admin(self):
        self.client.logout()
        resp = self.client.get(self._url('ai_employee_chat'))
        self.assertNotEqual(resp.status_code, 200)
