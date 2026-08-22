"""
Tests for the Prospect agent foundation — step 0 bug fixes plus the §1/§2/§6
groundwork (COLD_OUTREACH_AGENT.md + CORRECTIONS).

The outreach app had exactly one tested module before this (copy_guard,
and only because it had already shipped damage to real prospects). These
cover the behaviours that are expensive to get wrong: the sequence clock,
the pricing guardrail, the spend cap, and variant selection.
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from admin_dashboard.models import AIEmployee, AIEmployeeRun
from outreach import spend, variant_rotation
from outreach.copy_guard import describe_pricing_problems
from outreach.models import (
    EmailSent,
    EmailTemplateVariant,
    Lead,
    OutreachSettings,
)


class SequenceAdvanceTests(TestCase):
    """The step-0 fix: the sequence clock starts at SEND, not generation."""

    def setUp(self):
        self.lead = Lead.objects.create(
            firm_name='Test Firm', email='a@example.com',
            city='Austin', state='Texas')
        self.variant = EmailTemplateVariant.objects.get(
            sequence_step=1, name='Baseline')

    # Must be real-looking copy: the dispatcher runs copy_guard as a last
    # gate before SMTP, so a one-word placeholder gets correctly blocked
    # and never reaches the send path we are trying to exercise.
    BODY = (
        'Saw your site while looking at firms in Austin and noticed it '
        'loads slowly on mobile, which tends to cost enquiries. Worth a '
        'quick look? Happy to send over what I found either way.\n\n'
        '— Zachery')

    def _make_email(self, status, step=1, kind='cold'):
        return EmailSent.objects.create(
            lead=self.lead, kind=kind, status=status,
            subject='Quick note about your site', body=self.BODY,
            from_email='z@example.com',
            sequence_step=step, template_variant=self.variant)

    def test_pending_draft_does_not_advance_the_lead(self):
        """A draft awaiting approval must not start the follow-up clock."""
        self._make_email('pending_approval')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.sequence_step, 0)
        self.assertIsNone(self.lead.next_followup_at)

    def test_dispatch_advances_step_and_followup(self):
        email = self._make_email('approved')
        with patch('outreach.dispatcher._send_one'):
            from outreach.dispatcher import dispatch_approved_batch
            counts = dispatch_approved_batch()

        self.assertEqual(counts['sent'], 1)
        self.lead.refresh_from_db()
        email.refresh_from_db()
        self.assertEqual(email.status, 'sent')
        self.assertEqual(self.lead.sequence_step, 1)
        # Step 1 -> 2 is a 3-day gap, measured from the send.
        self.assertIsNotNone(self.lead.next_followup_at)
        delta = self.lead.next_followup_at - timezone.now()
        self.assertGreater(delta, datetime.timedelta(days=2, hours=23))
        self.assertLess(delta, datetime.timedelta(days=3, hours=1))

    def test_reply_send_never_moves_the_cold_pointer(self):
        """Reply drafts carry sequence_step=0 and must not rewind the lead."""
        self.lead.sequence_step = 2
        self.lead.save(update_fields=['sequence_step'])
        self._make_email('approved', step=0, kind='reply')

        with patch('outreach.dispatcher._send_one'):
            from outreach.dispatcher import dispatch_approved_batch
            dispatch_approved_batch()

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.sequence_step, 2)

    def test_dispatch_increments_variant_sends(self):
        self._make_email('approved')
        with patch('outreach.dispatcher._send_one'):
            from outreach.dispatcher import dispatch_approved_batch
            dispatch_approved_batch()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.sends, 1)


class EligibilityTests(TestCase):
    """Leads holding unsent mail must not be reconsidered — otherwise a
    backlog of unapproved drafts starves everyone else."""

    def test_lead_with_pending_draft_is_not_eligible(self):
        from outreach.sender import _eligible_leads

        lead = Lead.objects.create(
            firm_name='Holding Mail', email='h@example.com',
            city='Austin', state='Texas')
        self.assertIn(lead, _eligible_leads(now=timezone.now(), limit=10))

        EmailSent.objects.create(
            lead=lead, kind='cold', status='pending_approval',
            subject='s', body='b', from_email='z@example.com',
            sequence_step=1)
        self.assertNotIn(lead, _eligible_leads(now=timezone.now(), limit=10))


class PricingGuardrailTests(TestCase):
    """§1.1 — the agent may not invent a price."""

    def test_clean_copy_passes(self):
        self.assertEqual(
            describe_pricing_problems('Happy to take a look at your site.'),
            [])

    def test_invented_dollar_figure_is_rejected(self):
        problems = describe_pricing_problems(
            'I can rebuild it for $1,850 flat.')
        self.assertTrue(problems)
        self.assertIn('$1,850', problems[0])

    def test_discount_language_is_rejected(self):
        self.assertTrue(describe_pricing_problems('20% off if you sign now.'))
        self.assertTrue(describe_pricing_problems('I can waive the fee.'))

    def test_recurring_rate_is_rejected(self):
        self.assertTrue(
            describe_pricing_problems('Maintenance runs 450/month.'))

    def test_matching_active_tier_price_is_allowed(self):
        from billing.pricing_models import ServiceTier
        ServiceTier.objects.create(
            category='build', name='Essential Website Build',
            slug='essential-build', price=Decimal('2500.00'),
            price_display='$2,500', is_active=True)
        self.assertEqual(
            describe_pricing_problems('The Essential build is $2,500.'), [])

    def test_inactive_tier_price_is_still_rejected(self):
        from billing.pricing_models import ServiceTier
        ServiceTier.objects.create(
            category='build', name='Retired Tier', slug='retired',
            price=Decimal('999.00'), price_display='$999', is_active=False)
        self.assertTrue(describe_pricing_problems('Only $999 this month.'))

    def test_db_failure_fails_closed(self):
        """A DB hiccup must not become permission to quote a price."""
        with patch('outreach.copy_guard._approved_price_strings',
                   return_value=set()):
            self.assertTrue(describe_pricing_problems('It is $2,500.'))


class SpendCapTests(TestCase):
    """§1.3 — the daily ceiling reads today's runs, not a monthly rollup."""

    def setUp(self):
        self.employee = AIEmployee.objects.get(slug='prospect')
        cfg = OutreachSettings.load()
        cfg.daily_ai_spend_cap_usd = Decimal('5.00')
        cfg.save(update_fields=['daily_ai_spend_cap_usd'])

    def test_prospect_is_registered_and_starts_paused(self):
        self.assertEqual(self.employee.name, 'Prospect')
        self.assertFalse(self.employee.active)
        self.assertEqual(self.employee.reasoning_effort, 'medium')

    def test_no_spend_means_allowed(self):
        allowed, reason = spend.check_spend_allowed()
        self.assertTrue(allowed)
        self.assertEqual(reason, '')

    def test_spend_accumulates_across_runs_today(self):
        for amount in ('1.50', '2.00'):
            AIEmployeeRun.objects.create(
                employee=self.employee, trigger='scheduled',
                spend_usd=Decimal(amount))
        self.assertEqual(spend.spent_today(), Decimal('3.50'))
        self.assertTrue(spend.check_spend_allowed()[0])

    def test_cap_blocks_once_reached(self):
        AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled',
            spend_usd=Decimal('5.00'))
        allowed, reason = spend.check_spend_allowed()
        self.assertFalse(allowed)
        self.assertIn('Spend cap reached', reason)

    def test_in_flight_run_counts_against_the_cap(self):
        """A still-running run must count, or one long run walks past."""
        AIEmployeeRun.objects.create(
            employee=self.employee, trigger='manual',
            status='running', spend_usd=Decimal('6.00'))
        self.assertFalse(spend.check_spend_allowed()[0])

    def test_zero_cap_blocks_everything(self):
        cfg = OutreachSettings.load()
        cfg.daily_ai_spend_cap_usd = Decimal('0')
        cfg.save(update_fields=['daily_ai_spend_cap_usd'])
        self.assertFalse(spend.check_spend_allowed()[0])

    def test_yesterdays_spend_does_not_count(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled',
            spend_usd=Decimal('99.00'))
        AIEmployeeRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(spend.spent_today(), Decimal('0'))

    def test_known_model_is_priced(self):
        cost = spend.claude_call_cost_usd('claude-sonnet-5', 1_000_000, 0)
        self.assertEqual(cost, Decimal('2.00'))

    def test_unpriced_model_returns_zero(self):
        self.assertEqual(
            spend.claude_call_cost_usd('claude-not-real', 1_000_000, 0),
            Decimal('0'))


class PricingTableTests(TestCase):
    """Every model this codebase can emit must have a rate, or its spend
    silently reads as $0.00 and the cap never trips."""

    def test_every_emitted_model_is_priced(self):
        from reporting.ai import MODEL_CHAT, MODEL_CONTENT
        from reporting.models import CLAUDE_PRICING_USD_PER_MTOK

        for model in (MODEL_CONTENT, MODEL_CHAT, 'claude-sonnet-4-6'):
            self.assertIn(
                model, CLAUDE_PRICING_USD_PER_MTOK,
                f'{model} is emitted by this codebase but has no rate — its '
                f'cost would read $0.00 and the daily spend cap would '
                f'under-report.')


class VariantRotationTests(TestCase):
    """§6 — most-active default, weighting dormant until volume justifies."""

    def setUp(self):
        self.baseline = EmailTemplateVariant.objects.get(
            sequence_step=1, name='Baseline')

    def test_baseline_variants_seeded_active_for_all_four_steps(self):
        for step in (1, 2, 3, 4):
            v = EmailTemplateVariant.objects.get(
                sequence_step=step, name='Baseline')
            self.assertTrue(v.active)
            self.assertEqual(v.proposed_by, 'human')
            self.assertTrue(v.angle_instructions.strip())

    def test_single_variant_is_chosen(self):
        variant, reason = variant_rotation.choose_variant(1)
        self.assertEqual(variant, self.baseline)
        self.assertIn('only active variant', reason)

    def test_no_active_variant_returns_none(self):
        EmailTemplateVariant.objects.filter(sequence_step=1).update(
            active=False)
        variant, reason = variant_rotation.choose_variant(1)
        self.assertIsNone(variant)
        self.assertIn('No active template variant', reason)

    def test_agent_proposed_variants_default_inactive(self):
        v = EmailTemplateVariant.objects.create(
            name='Speed angle', sequence_step=1,
            angle_instructions='Lead on load time.', proposed_by='agent')
        self.assertFalse(v.active)
        # ...and therefore is not selectable until a human flips it.
        chosen, _ = variant_rotation.choose_variant(1)
        self.assertEqual(chosen, self.baseline)

    def test_most_active_wins_while_weighting_is_off(self):
        self.baseline.sends = 40
        self.baseline.save(update_fields=['sends'])
        rival = EmailTemplateVariant.objects.create(
            name='Rival', sequence_step=1, active=True,
            angle_instructions='Different angle.', sends=5)

        self.assertFalse(variant_rotation.WEIGHTED_ROTATION_ENABLED)
        chosen, reason = variant_rotation.choose_variant(1)
        self.assertEqual(chosen, self.baseline)
        self.assertIn('most-established', reason)
        self.assertNotEqual(chosen, rival)

    def test_weights_are_equal_below_min_sample(self):
        rival = EmailTemplateVariant.objects.create(
            name='Rival', sequence_step=1, active=True,
            angle_instructions='x', sends=5, replies=5)
        weights = variant_rotation.compute_weights([self.baseline, rival])
        self.assertAlmostEqual(weights[self.baseline.pk], 0.5)
        self.assertAlmostEqual(weights[rival.pk], 0.5)

    def test_weights_favour_better_reply_rate_above_min_sample(self):
        self.baseline.sends = 100
        self.baseline.replies = 2
        self.baseline.save(update_fields=['sends', 'replies'])
        rival = EmailTemplateVariant.objects.create(
            name='Rival', sequence_step=1, active=True,
            angle_instructions='x', sends=100, replies=10)

        weights = variant_rotation.compute_weights([self.baseline, rival])
        self.assertGreater(weights[rival.pk], weights[self.baseline.pk])
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)
        # Floor allocation protects the loser from being starved out.
        floor = variant_rotation.FLOOR_ALLOCATION / 2
        self.assertGreaterEqual(weights[self.baseline.pk], floor)

    def test_counters_increment_atomically(self):
        variant_rotation.record_send(self.baseline.pk)
        variant_rotation.record_open(self.baseline.pk)
        variant_rotation.record_reply(self.baseline.pk)
        self.baseline.refresh_from_db()
        self.assertEqual(
            (self.baseline.sends, self.baseline.opens, self.baseline.replies),
            (1, 1, 1))

    def test_counter_bump_tolerates_missing_variant(self):
        variant_rotation.record_send(None)
        variant_rotation.record_send(999999)  # must not raise


class AgentLoopTests(TestCase):
    """§5.1 — the loop primitive."""

    def test_defaults_leave_room_for_thinking(self):
        from reporting import ai
        self.assertGreaterEqual(ai._DEFAULT_AGENT_MAX_TOKENS, 8000)

    def test_tool_failure_becomes_a_result_not_an_exception(self):
        from reporting.ai import _run_agent_tool

        def boom(name, payload):
            raise RuntimeError('exploded')

        output, is_error = _run_agent_tool(boom, 'queue_apify_search', {})
        self.assertTrue(is_error)
        self.assertIn('exploded', output)

    def test_dict_tool_output_is_serialised(self):
        from reporting.ai import _run_agent_tool
        output, is_error = _run_agent_tool(
            lambda n, p: {'found': 3}, 'get_pending_work', {})
        self.assertFalse(is_error)
        self.assertIn('found', output)


class FirstTextBlockTests(TestCase):
    """Sonnet 5 returns [ThinkingBlock, TextBlock] — index 0 is not text."""

    class _Block:
        def __init__(self, type_, **kw):
            self.type = type_
            for k, v in kw.items():
                setattr(self, k, v)

    class _Response:
        def __init__(self, content, stop_reason='end_turn'):
            self.content = content
            self.stop_reason = stop_reason

    def test_skips_thinking_block(self):
        from reporting.ai import _first_text_block
        resp = self._Response([
            self._Block('thinking', thinking='hmm...'),
            self._Block('text', text='  the answer  '),
        ])
        self.assertEqual(_first_text_block(resp), 'the answer')

    def test_no_text_block_returns_empty(self):
        from reporting.ai import _first_text_block
        resp = self._Response(
            [self._Block('thinking', thinking='...')], stop_reason='max_tokens')
        self.assertEqual(_first_text_block(resp), '')


class PermanentFailureTests(TestCase):
    """Undeliverable addresses must stop, not retry every 30 minutes."""

    def setUp(self):
        self.lead = Lead.objects.create(
            firm_name='Bad Address Co', email='nope@invalid.example',
            city='Austin', state='Texas')
        self.email = EmailSent.objects.create(
            lead=self.lead, kind='cold', status='approved',
            subject='Quick note about your site',
            body=SequenceAdvanceTests.BODY,
            from_email='z@example.com', sequence_step=1)

    def _dispatch(self, exc):
        from outreach.dispatcher import dispatch_approved_batch
        with patch('outreach.dispatcher._send_one', side_effect=exc):
            return dispatch_approved_batch()

    def test_permanent_failure_rejects_and_suppresses(self):
        import smtplib
        counts = self._dispatch(smtplib.SMTPRecipientsRefused(
            {'nope@invalid.example': (550, b'no such user')}))

        self.assertEqual(counts['permanent_failure'], 1)
        self.assertEqual(counts['failed'], 0)
        self.email.refresh_from_db()
        self.assertEqual(self.email.status, 'rejected')
        from outreach.models import SuppressionList
        self.assertTrue(
            SuppressionList.objects.filter(
                email='nope@invalid.example').exists())

    def test_permanent_failure_does_not_unsubscribe_the_lead(self):
        """Suppression is per-address so re-enrichment can still save the
        firm; killing the lead over a typo throws the prospect away."""
        import smtplib
        self._dispatch(smtplib.SMTPSenderRefused(550, b'no', 'z@example.com'))
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.unsubscribed)

    def test_transient_failure_leaves_row_approved_for_retry(self):
        import smtplib
        counts = self._dispatch(smtplib.SMTPServerDisconnected('conn reset'))

        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['permanent_failure'], 0)
        self.email.refresh_from_db()
        self.assertEqual(self.email.status, 'approved')

    def test_unknown_error_defaults_to_transient(self):
        """Retrying is the cheaper mistake."""
        counts = self._dispatch(RuntimeError('something odd'))
        self.assertEqual(counts['failed'], 1)
        self.email.refresh_from_db()
        self.assertEqual(self.email.status, 'approved')

    def test_smtp_5xx_code_is_permanent(self):
        import smtplib
        counts = self._dispatch(
            smtplib.SMTPDataError(550, b'mailbox unavailable'))
        self.assertEqual(counts['permanent_failure'], 1)

    def test_smtp_4xx_code_is_transient(self):
        import smtplib
        counts = self._dispatch(
            smtplib.SMTPDataError(451, b'greylisted, try later'))
        self.assertEqual(counts['failed'], 1)


class EnricherExtractionTests(TestCase):
    """3b — entity decoding and a fullmatch guard on the winning address."""

    def _extract(self, html):
        from outreach.enricher import _extract_from_html
        lead = Lead.objects.create(firm_name='X', city='Austin', state='Texas')
        _extract_from_html(lead, html, 'https://example.com/')
        return lead

    def test_html_entity_encoded_email_is_found(self):
        lead = self._extract('<p>Reach us at info&#64;firm&#46;com today</p>')
        self.assertEqual(lead.email, 'info@firm.com')

    def test_entity_encoded_copyright_year_is_found(self):
        lead = self._extract('<footer>&#169; 2019 The Firm</footer>')
        self.assertEqual(lead.copyright_year, 2019)

    def test_mailto_query_string_is_stripped(self):
        lead = self._extract(
            '<a href="mailto:hi@firm.com?subject=Hello%20there">Email</a>')
        self.assertEqual(lead.email, 'hi@firm.com')

    def test_trailing_punctuation_is_stripped(self):
        lead = self._extract('<p>Contact hello@firm.com.</p>')
        self.assertEqual(lead.email, 'hello@firm.com')

    def test_noreply_is_skipped_for_a_real_address(self):
        lead = self._extract(
            '<p>noreply@firm.com</p><p>partners@firm.com</p>')
        self.assertEqual(lead.email, 'partners@firm.com')

    def test_generic_provider_is_flagged(self):
        lead = self._extract('<p>thefirm&#64;gmail.com</p>')
        self.assertEqual(lead.email, 'thefirm@gmail.com')
        self.assertTrue(lead.has_generic_email)


class ApifyQuotaTests(TestCase):
    """1.3 cap B — a separate pool from the Claude cap."""

    def test_default_quota_allows_a_run(self):
        allowed, reason = spend.check_apify_allowed()
        self.assertTrue(allowed)
        self.assertEqual(reason, '')

    def test_zero_runs_disables_sourcing(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_runs_per_day = 0
        cfg.save(update_fields=['apify_max_runs_per_day'])
        allowed, reason = spend.check_apify_allowed()
        self.assertFalse(allowed)
        self.assertIn('quota reached', reason.lower())

    def test_apify_quota_is_independent_of_the_claude_cap(self):
        """Exhausting the LLM budget must not disable sourcing — one
        runaway scrape cannot eat the reasoning budget."""
        employee = AIEmployee.objects.get(slug='prospect')
        AIEmployeeRun.objects.create(
            employee=employee, trigger='scheduled',
            spend_usd=Decimal('999.00'))

        self.assertFalse(spend.check_spend_allowed()[0])
        self.assertTrue(spend.check_apify_allowed()[0])

    def test_llm_cap_default_is_ten_dollars(self):
        self.assertEqual(spend.daily_cap(), Decimal('10.00'))

    def test_result_ceiling_is_exposed(self):
        # Asserted against the configured value rather than a literal:
        # this default moved from 100 to 50 once the actor's real pricing
        # ($0.002/lead against a $5/month plan) was known, and pinning a
        # magic number here just breaks on every future tuning.
        # tests_apify::test_default_quota_fits_the_monthly_plan is what
        # actually guards the budget.
        cfg = OutreachSettings.load()
        self.assertEqual(
            spend.apify_max_results_per_run(), cfg.apify_max_results_per_run)
        self.assertGreater(spend.apify_max_results_per_run(), 0)


class MessageHistoryTests(TestCase):
    """5.1 — store replayable message shape, not a flattened summary."""

    class _Block:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    def test_dict_blocks_pass_through(self):
        from reporting.ai import _serialise_content_blocks
        blocks = [{'type': 'text', 'text': 'hi'}]
        self.assertEqual(_serialise_content_blocks(blocks), blocks)

    def test_tool_use_block_keeps_id_name_and_input(self):
        from reporting.ai import _serialise_content_blocks
        out = _serialise_content_blocks([
            self._Block(type='tool_use', id='tu_1', name='research_lead',
                        input={'lead_id': 7}),
        ])
        self.assertEqual(out, [{
            'type': 'tool_use', 'id': 'tu_1',
            'name': 'research_lead', 'input': {'lead_id': 7},
        }])

    def test_output_is_json_serialisable(self):
        import json

        from reporting.ai import _serialise_content_blocks
        out = _serialise_content_blocks([
            self._Block(type='text', text='thinking about it'),
            self._Block(type='tool_use', id='tu_2', name='log_note',
                        input={'note': 'x'}),
        ])
        json.loads(json.dumps(out))  # must not raise

    def test_run_stores_history_as_real_message_dicts(self):
        employee = AIEmployee.objects.get(slug='prospect')
        history = [
            {'role': 'user', 'content': 'Find work.'},
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'tu_1',
                 'name': 'get_pending_work', 'input': {}},
            ]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tu_1',
                 'content': '3 leads', 'is_error': False},
            ]},
        ]
        run = AIEmployeeRun.objects.create(
            employee=employee, trigger='manual',
            summary='Checked pending work.', message_history=history)
        run.refresh_from_db()

        self.assertEqual(run.message_history, history)
        # summary stays a separate human-readable field, not a substitute.
        self.assertEqual(run.summary, 'Checked pending work.')
        self.assertEqual(
            run.message_history[1]['content'][0]['name'], 'get_pending_work')

    def test_history_defaults_to_empty_list(self):
        employee = AIEmployee.objects.get(slug='prospect')
        run = AIEmployeeRun.objects.create(
            employee=employee, trigger='scheduled')
        self.assertEqual(run.message_history, [])


class BookingAttributionTests(TestCase):
    """Credit a booked call back to the angle that earned it."""

    def setUp(self):
        self.variant = EmailTemplateVariant.objects.get(
            sequence_step=1, name='Baseline')
        self.lead = Lead.objects.create(
            firm_name='Booked Co', email='booker@example.com',
            city='Austin', state='Texas')

    def _sent(self, step, variant, sent_at):
        e = EmailSent.objects.create(
            lead=self.lead, kind='cold', status='sent',
            subject='s', body=SequenceAdvanceTests.BODY,
            from_email='z@example.com', sequence_step=step,
            template_variant=variant)
        EmailSent.objects.filter(pk=e.pk).update(sent_at=sent_at)
        return e

    def test_booking_credits_the_most_recent_sent_variant(self):
        from scheduler.views import _attribute_booking_to_variant

        other = EmailTemplateVariant.objects.create(
            name='Speed', sequence_step=2, active=True,
            angle_instructions='x')
        now = timezone.now()
        self._sent(1, self.variant, now - datetime.timedelta(days=5))
        self._sent(2, other, now - datetime.timedelta(days=1))

        credited = _attribute_booking_to_variant('booker@example.com')
        self.assertEqual(credited, other.pk)
        other.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(other.bookings, 1)
        self.assertEqual(self.variant.bookings, 0)

    def test_booking_matches_case_insensitively(self):
        from scheduler.views import _attribute_booking_to_variant
        self._sent(1, self.variant, timezone.now())
        self.assertEqual(
            _attribute_booking_to_variant('BOOKER@EXAMPLE.COM'),
            self.variant.pk)

    def test_inbound_booking_with_no_outreach_credits_nothing(self):
        from scheduler.views import _attribute_booking_to_variant
        self.assertIsNone(
            _attribute_booking_to_variant('never-emailed@example.com'))

    def test_unsent_email_does_not_count(self):
        from scheduler.views import _attribute_booking_to_variant
        EmailSent.objects.create(
            lead=self.lead, kind='cold', status='pending_approval',
            subject='s', body=SequenceAdvanceTests.BODY,
            from_email='z@example.com', sequence_step=1,
            template_variant=self.variant)
        self.assertIsNone(
            _attribute_booking_to_variant('booker@example.com'))


class FrozenSequenceHealthTests(TestCase):
    """A silent sequence freeze must surface as a red row."""

    def setUp(self):
        self.lead = Lead.objects.create(
            firm_name='Frozen Co', email='f@example.com',
            city='Austin', state='Texas')

    def _section(self):
        from admin_dashboard.data_health_views import (
            _outreach_sequence_section,
        )
        return _outreach_sequence_section()

    def test_clean_when_lead_advanced(self):
        self.lead.sequence_step = 1
        self.lead.save(update_fields=['sequence_step'])
        EmailSent.objects.create(
            lead=self.lead, kind='cold', status='sent',
            subject='s', body=SequenceAdvanceTests.BODY,
            from_email='z@example.com', sequence_step=1)
        section = self._section()
        self.assertEqual(section['frozen_count'], 0)
        self.assertTrue(section['clean'])

    def test_detects_sent_email_whose_lead_never_advanced(self):
        EmailSent.objects.create(
            lead=self.lead, kind='cold', status='sent',
            subject='s', body=SequenceAdvanceTests.BODY,
            from_email='z@example.com', sequence_step=1)
        section = self._section()
        self.assertEqual(section['frozen_count'], 1)
        self.assertFalse(section['clean'])

    def test_unsent_mail_is_not_flagged(self):
        EmailSent.objects.create(
            lead=self.lead, kind='cold', status='pending_approval',
            subject='s', body=SequenceAdvanceTests.BODY,
            from_email='z@example.com', sequence_step=1)
        self.assertEqual(self._section()['frozen_count'], 0)
