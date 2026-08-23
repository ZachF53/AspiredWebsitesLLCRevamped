"""
Tests for the Instantly pipeline: verify -> icebreak -> push -> webhook.

The cases here are drawn from what actually went wrong on prod rather
than from imagination. Where a test encodes a real failure, the failure
is named in the docstring so nobody later "simplifies" the assertion
that exists to stop it recurring.
"""

import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from outreach import icebreaker, instantly, verify
from outreach.models import (
    EmailReply, InstantlyEvent, Lead, OutreachCampaign, SuppressionList,
)


def make_lead(**kw):
    defaults = {
        'firm_name': 'Chen Law Group',
        'attorney_name': 'Sarah Chen',
        'email': 'sarah@chenlawgroup.com',
        'city': 'Austin',
        'state': 'TX',
        'source': 'apify',
        'email_verification_status': verify.VALID,
        'icebreaker': 'Your intake form posts over plain http.',
    }
    defaults.update(kw)
    return Lead.objects.create(**defaults)


def allow_sending():
    """Open both send gates for tests about the OTHER push gates.

    Explicit rather than a global default: a test that pushes leads
    should have to say out loud that sending is permitted, because in
    production that is two deliberate decisions and never an ambient
    condition.
    """
    return patch.object(instantly, 'sending_allowed', return_value=(True, ''))


# ── verify.py ──────────────────────────────────────────────────────────

class RoleAddressTests(TestCase):
    """111 of prod's 416 sends went to info@. None ever should have."""

    def test_the_actual_prod_addresses_are_blocked(self):
        # Every one of these was pulled from the live prod lead table.
        for addr in ('info@emergencydentalservice.com',
                     'contact@designscapessa.com',
                     'lawassistant@danielslawfirm.org',
                     'mail@ksfamilylaw.com',
                     'hello@hellotend.com',
                     'hey@mandr-group.com',
                     'office@19thstreetdental.com'):
            with self.subTest(addr=addr):
                self.assertEqual(verify.screen(addr), verify.ROLE)
                self.assertFalse(verify.is_sendable(verify.ROLE))

    def test_real_people_are_not_blocked(self):
        """The substring trap: 'sales.director@' is a person, 'sales@' is not.

        An earlier draft matched role words as substrings, which would
        have silently discarded real decision-makers.
        """
        for addr in ('sales.director@firm.com',
                     'administer@firm.com',
                     'infosec@firm.com',
                     'contactor@firm.com',
                     'sarah@chenlawgroup.com',
                     'j.helpman@firm.com'):
            with self.subTest(addr=addr):
                self.assertNotEqual(verify.screen(addr), verify.ROLE)

    def test_separator_spellings_collapse_to_one_inbox(self):
        for addr in ('no-reply@x.com', 'no.reply@x.com', 'noreply@x.com',
                     'front.desk@x.com', 'frontdesk@x.com'):
            with self.subTest(addr=addr):
                self.assertEqual(verify.screen(addr), verify.ROLE)

    def test_plus_tags_do_not_evade_the_filter(self):
        self.assertEqual(verify.screen('info+leads@firm.com'), verify.ROLE)

    def test_html_mangled_address_is_invalid(self):
        """This exact string is in the prod lead table.

        It came from enricher.py scraping an un-unescaped HTML attribute.
        It must never be treated as a deliverable address.
        """
        self.assertEqual(
            verify.screen('getfit@apexfitnesssa.com&quot;,'), verify.INVALID)

    def test_consumer_mailbox_is_flagged_but_allowed(self):
        """A solo practitioner on gmail is a real prospect."""
        self.assertEqual(verify.screen('sarah@gmail.com'), verify.CONSUMER)
        self.assertTrue(verify.is_sendable(verify.CONSUMER))

    def test_disposable_is_rejected(self):
        self.assertEqual(
            verify.screen('x@mailinator.com'), verify.INVALID)


class SendGateTests(TestCase):
    """is_sendable is the last word on whether an address may be used."""

    def test_pending_is_never_sendable(self):
        """Unverified-by-omission must not read as approved."""
        self.assertFalse(verify.is_sendable(verify.PENDING))

    @override_settings(EMAIL_VERIFY_REQUIRED=True)
    def test_unverified_blocked_when_verification_required(self):
        self.assertFalse(verify.is_sendable(verify.UNVERIFIED))

    @override_settings(EMAIL_VERIFY_REQUIRED=False)
    def test_unverified_allowed_only_by_explicit_opt_out(self):
        self.assertTrue(verify.is_sendable(verify.UNVERIFIED))

    @override_settings(EMAIL_VERIFY_ALLOW_CATCH_ALL=False)
    def test_catch_all_blocked_by_default(self):
        """A catch-all accepts at SMTP time and bounces later."""
        self.assertFalse(verify.is_sendable(verify.RISKY))

    @override_settings(EMAIL_VERIFY_PROVIDER='', EMAIL_VERIFY_API_KEY='')
    def test_role_check_works_with_no_provider_configured(self):
        """Role suppression must not depend on a vendor account."""
        self.assertEqual(verify.verify_email('info@firm.com'), verify.ROLE)

    @override_settings(EMAIL_VERIFY_PROVIDER='millionverifier',
                       EMAIL_VERIFY_API_KEY='k')
    def test_role_address_never_costs_an_api_credit(self):
        with patch('outreach.verify._verify_millionverifier') as mv:
            self.assertEqual(verify.verify_email('info@firm.com'), verify.ROLE)
        mv.assert_not_called()

    @override_settings(EMAIL_VERIFY_PROVIDER='millionverifier',
                       EMAIL_VERIFY_API_KEY='k')
    def test_unrecognised_vendor_verdict_fails_closed(self):
        with patch('outreach.verify.requests.get') as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {'result': 'something_new'}
            self.assertEqual(
                verify.verify_email('sarah@firm.com'), verify.RISKY)

    @override_settings(EMAIL_VERIFY_PROVIDER='millionverifier',
                       EMAIL_VERIFY_API_KEY='k')
    def test_provider_outage_leaves_status_untouched_for_retry(self):
        """A vendor 500 must not permanently mark a good lead invalid."""
        lead = make_lead(email_verification_status=verify.PENDING)
        with patch('outreach.verify.requests.get') as get:
            get.return_value.status_code = 500
            get.return_value.text = 'upstream boom'
            verify.verify_lead(lead)
        lead.refresh_from_db()
        self.assertEqual(lead.email_verification_status, verify.PENDING)


# ── icebreaker.py ──────────────────────────────────────────────────────

class IcebreakerGuardTests(TestCase):
    """An invented detail is worse than a generic line — it is checkable."""

    def test_fabricated_claim_is_rejected(self):
        lead = make_lead()
        for line in ('I just listened to your podcast episode on trial prep.',
                     'Congratulations on your recent award!',
                     'We worked with a firm like yours and got 40% more leads.'):
            with self.subTest(line=line):
                self.assertTrue(icebreaker.describe_problems(line, lead))

    def test_score_not_measured_is_rejected(self):
        """Citing a PageSpeed score we never measured is fabrication."""
        lead = make_lead(website_performance_score=None)
        problems = icebreaker.describe_problems(
            'Your site scores 34/100 on Google PageSpeed.', lead)
        self.assertTrue(any('never measured' in p for p in problems))

    def test_measured_score_is_allowed(self):
        lead = make_lead(website_performance_score=34)
        self.assertEqual(
            icebreaker.describe_problems(
                'Your site scores 34/100 on Google PageSpeed.', lead), [])

    def test_wrong_copyright_year_is_rejected(self):
        lead = make_lead(copyright_year=2019)
        problems = icebreaker.describe_problems(
            'Your footer still says 2021.', lead)
        self.assertTrue(problems)

    def test_model_commentary_is_rejected(self):
        """The line is sent verbatim; commentary would go to the prospect."""
        lead = make_lead()
        problems = icebreaker.describe_problems(
            "Here's a great opening line for this lead:", lead)
        self.assertTrue(problems)

    def test_observations_only_report_measured_signals(self):
        lead = make_lead(has_ssl=False, website_performance_score=20,
                         copyright_year=2018)
        keys = {k for k, _ in icebreaker.observations(lead)}
        self.assertIn('no_ssl', keys)
        self.assertIn('slow', keys)
        self.assertIn('stale_copyright', keys)

    def test_no_observations_when_nothing_measured(self):
        lead = make_lead(has_ssl=None, website_performance_score=None,
                         copyright_year=None, website='https://x.com')
        self.assertEqual(icebreaker.observations(lead), [])


# ── instantly.push_leads ───────────────────────────────────────────────

@override_settings(INSTANTLY_TOKEN='t', INSTANTLY_MAX_PUSH_PER_DAY=200)
class PushGateTests(TestCase):
    """push_leads is the last gate before an address becomes a real send."""

    def setUp(self):
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law', slug='tx-law', niche='law firm',
            instantly_campaign_id='camp-1', active=True)

    def _push(self, leads):
        with allow_sending(), patch.object(
                instantly, '_request',
                return_value={'id': 'inst-1'}) as req:
            summary = instantly.push_leads(leads, self.campaign)
        return summary, req

    def test_role_address_is_never_pushed(self):
        lead = make_lead(email='info@firm.com',
                         email_verification_status=verify.ROLE)
        summary, req = self._push([lead])
        self.assertEqual(summary['pushed'], 0)
        self.assertEqual(summary['skipped_unsendable'], 1)
        req.assert_not_called()

    def test_lead_without_icebreaker_is_not_pushed(self):
        """No personalised line means it is a mail merge."""
        lead = make_lead(icebreaker='')
        summary, req = self._push([lead])
        self.assertEqual(summary['pushed'], 0)
        self.assertEqual(summary['skipped_no_icebreaker'], 1)
        req.assert_not_called()

    def test_suppressed_address_is_not_pushed(self):
        """Unsubscribes are permanent — CLAUDE.md business rule 6."""
        lead = make_lead()
        SuppressionList.objects.create(email=lead.email, reason='unsub')
        summary, _ = self._push([lead])
        self.assertEqual(summary['skipped_suppressed'], 1)

    def test_unsubscribed_lead_is_not_pushed(self):
        lead = make_lead(unsubscribed=True)
        summary, _ = self._push([lead])
        self.assertEqual(summary['skipped_unsubscribed'], 1)

    def test_already_pushed_lead_is_not_pushed_twice(self):
        lead = make_lead(instantly_lead_id='inst-existing')
        summary, req = self._push([lead])
        self.assertEqual(summary['skipped_already_pushed'], 1)
        req.assert_not_called()

    def test_good_lead_is_pushed_and_recorded(self):
        lead = make_lead()
        summary, req = self._push([lead])
        self.assertEqual(summary['pushed'], 1)
        req.assert_called_once()
        lead.refresh_from_db()
        self.assertEqual(lead.instantly_lead_id, 'inst-1')
        self.assertEqual(lead.campaign_id, self.campaign.pk)
        self.assertEqual(lead.status, 'contacted')
        self.assertIsNotNone(lead.pushed_to_instantly_at)

    def test_icebreaker_travels_as_a_custom_variable(self):
        lead = make_lead()
        _, req = self._push([lead])
        payload = req.call_args.kwargs['json']
        self.assertEqual(
            payload['custom_variables']['icebreaker'], lead.icebreaker)
        self.assertEqual(payload['first_name'], 'Sarah')
        self.assertEqual(payload['last_name'], 'Chen')

    def test_paused_campaign_refuses_the_push(self):
        self.campaign.active = False
        self.campaign.save()
        with allow_sending():
            with self.assertRaises(instantly.InstantlyError):
                instantly.push_leads([make_lead()], self.campaign)

    def test_campaign_without_instantly_id_refuses_the_push(self):
        self.campaign.instantly_campaign_id = ''
        self.campaign.save()
        with allow_sending():
            with self.assertRaises(instantly.InstantlyError):
                instantly.push_leads([make_lead()], self.campaign)

    @override_settings(INSTANTLY_MAX_PUSH_PER_DAY=2)
    def test_daily_push_cap_defers_the_remainder(self):
        leads = [make_lead(email=f'a{i}@firm{i}.com',
                           firm_name=f'Firm {i}') for i in range(5)]
        summary, _ = self._push(leads)
        self.assertEqual(summary['pushed'], 2)

    @override_settings(INSTANTLY_TOKEN='')
    def test_missing_token_is_a_clear_refusal(self):
        with self.assertRaises(instantly.InstantlyNotConfigured):
            instantly.list_campaigns()


# ── webhook ────────────────────────────────────────────────────────────

@override_settings(INSTANTLY_WEBHOOK_SECRET='s3cret', INSTANTLY_TOKEN='t')
class WebhookTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse('instantly_events', args=['s3cret'])
        self.lead = make_lead(instantly_lead_id='inst-1')

    def post(self, payload, url=None):
        return self.client.post(
            url or self.url, data=json.dumps(payload),
            content_type='application/json')

    def test_bad_secret_is_forbidden(self):
        bad = reverse('instantly_events', args=['wrong'])
        self.assertEqual(self.post({'event_type': 'email_sent'}, bad)
                         .status_code, 403)

    @override_settings(INSTANTLY_WEBHOOK_SECRET='')
    def test_unset_secret_refuses_everything(self):
        """Fail closed: an anon POST here can mark leads unsubscribed."""
        self.assertEqual(self.post({'event_type': 'email_sent'})
                         .status_code, 403)

    def test_own_domain_reply_is_not_ingested(self):
        """THE BUG, reproduced exactly.

        Prod has a Lead row for 'Aspired AI LLC' carrying
        hello@aspired-ai.com, so ten Google Ads billing notifications
        MATCHED a lead and were filed as prospect replies. The lead is
        created here on purpose — without it the event would be dropped
        for the unrelated reason that no lead matched, and the test would
        pass while proving nothing.
        """
        Lead.objects.create(
            firm_name='Aspired AI LLC', email='hello@aspired-ai.com',
            source='manual')
        resp = self.post({
            'event_type': 'reply_received',
            'lead_email': 'hello@aspired-ai.com',
            'reply_text': 'Progress toward the $1,000 credit: ~$494 of $500.',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(EmailReply.objects.count(), 0)
        event = InstantlyEvent.objects.get()
        self.assertIn('our own domain', event.error)

    def test_autoresponder_is_not_ingested(self):
        resp = self.post({
            'event_type': 'reply_received',
            'lead_email': self.lead.email,
            'reply_subject': 'Automatic reply: out of office',
            'reply_text': 'I am out of the office until Monday.',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(EmailReply.objects.count(), 0)

    def test_no_reply_sender_is_not_ingested(self):
        resp = self.post({
            'event_type': 'reply_received',
            'lead_email': 'no-reply@somecompany.com',
            'reply_text': 'This mailbox is unmonitored.',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(EmailReply.objects.count(), 0)

    def test_reply_from_a_lead_we_never_emailed_is_not_ingested(self):
        resp = self.post({
            'event_type': 'reply_received',
            'lead_email': 'stranger@nowhere.com',
            'reply_text': 'who is this',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(EmailReply.objects.count(), 0)

    def test_genuine_reply_is_ingested_and_pauses_the_lead(self):
        with patch('outreach.instantly_webhook._pause_quietly'), \
             patch('outreach.tasks.classify_and_draft_reply_task.delay'):
            resp = self.post({
                'event_type': 'reply_received',
                'lead_email': self.lead.email,
                'reply_subject': 'Re: your site',
                'reply_text': 'Interesting, tell me more.',
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(EmailReply.objects.count(), 1)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'replied')
        self.assertTrue(self.lead.sequence_paused)

    def test_duplicate_delivery_creates_one_reply(self):
        """Webhooks are at-least-once; a retry must not double-file."""
        payload = {
            'event_type': 'reply_received',
            'id': 'evt-42',
            'lead_email': self.lead.email,
            'reply_text': 'Sure, send details.',
        }
        with patch('outreach.instantly_webhook._pause_quietly'), \
             patch('outreach.tasks.classify_and_draft_reply_task.delay'):
            first = self.post(payload)
            second = self.post(payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['status'], 'duplicate')
        self.assertEqual(EmailReply.objects.count(), 1)
        self.assertEqual(InstantlyEvent.objects.count(), 1)

    def test_bounce_suppresses_the_address_permanently(self):
        self.post({'event_type': 'email_bounced',
                   'lead_email': self.lead.email})
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.email_verification_status, verify.INVALID)
        self.assertTrue(
            SuppressionList.objects.filter(email=self.lead.email).exists())

    def test_unsubscribe_is_permanent(self):
        with patch('outreach.instantly_webhook._pause_quietly'):
            self.post({'event_type': 'lead_unsubscribed',
                       'lead_email': self.lead.email})
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.unsubscribed)
        self.assertEqual(self.lead.status, 'unsubscribed')
        self.assertTrue(
            SuppressionList.objects.filter(email=self.lead.email).exists())

    def test_email_sent_advances_the_sequence_clock(self):
        """Under SendGrid this advanced at GENERATION time, which froze
        the funnel when a draft was never dispatched. It now anchors to a
        confirmed send."""
        self.post({'event_type': 'email_sent',
                   'lead_email': self.lead.email, 'step': 2})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.sequence_step, 2)
        self.assertIsNotNone(self.lead.last_contacted_at)

    def test_unknown_event_is_stored_not_dropped(self):
        resp = self.post({'event_type': 'some_new_thing_2027',
                          'lead_email': self.lead.email})
        self.assertEqual(resp.status_code, 200)
        event = InstantlyEvent.objects.get()
        self.assertEqual(event.event_type, 'unknown')
        self.assertEqual(event.raw_event_type, 'some_new_thing_2027')

    def test_non_json_body_is_rejected(self):
        resp = self.client.post(self.url, data='not json',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


# ── enricher match verification ────────────────────────────────────────

class BusinessMatchTests(TestCase):
    """Measured 2026-08-22: taking the first Facebook hit was ~50% wrong."""

    def test_phone_mismatch_rejects_the_hit(self):
        lead = make_lead(firm_name='Godwin Law Office', phone='512-555-0100')
        from outreach.enricher import _matches_business
        ok, why = _matches_business(
            {'title': 'Goodwin & Goodwin, LLP | Charleston',
             'description': 'Call us at (843) 555-9999'}, lead)
        self.assertFalse(ok)
        self.assertIn('phone', why)

    def test_phone_match_accepts_the_hit(self):
        lead = make_lead(firm_name='Godwin Law Office', phone='512-555-0100')
        from outreach.enricher import _matches_business
        ok, why = _matches_business(
            {'title': 'Godwin Law', 'description': 'Call (512) 555-0100'},
            lead)
        self.assertTrue(ok)
        self.assertIn('phone', why)

    def test_generic_words_do_not_create_false_matches(self):
        """'Law Office' matching 'Law Group' would match everything."""
        lead = make_lead(firm_name='Chen Law Group', phone='')
        from outreach.enricher import _matches_business
        ok, _ = _matches_business(
            {'title': 'Martinez Law Office', 'description': ''}, lead)
        self.assertFalse(ok)

    def test_name_variation_still_matches(self):
        lead = make_lead(firm_name='Chen Law Group', phone='')
        from outreach.enricher import _matches_business
        ok, _ = _matches_business(
            {'title': 'The Law Office of Sarah Chen', 'description': ''},
            lead)
        self.assertTrue(ok)


# ── sequence copy ──────────────────────────────────────────────────────

class SequenceCopyTests(TestCase):
    """The copy is the control half of every send. It has to stay clean."""

    ADDR = '1 Main St, San Antonio, TX 78205'

    def test_texas_law_copy_passes_preflight(self):
        from outreach import sequences
        steps = sequences.build_steps('texas-law', self.ADDR)
        self.assertEqual(sequences.describe_problems(steps), [])

    @override_settings(COMPANY_POSTAL_ADDRESS='')
    def test_no_postal_address_refuses_to_build(self):
        """CAN-SPAM requires it; forgetting must be impossible, not likely.

        The setting is overridden to empty because a real address now
        lives in .env -- without the override this test would pass by
        falling back to that value and would prove nothing.
        """
        from outreach import sequences
        with self.assertRaises(sequences.SequenceError):
            sequences.build_steps('texas-law', '')

    def test_footer_is_on_every_touch(self):
        from outreach import sequences
        for step in sequences.build_steps('texas-law', self.ADDR):
            self.assertIn('Aspired Websites LLC', step['body'])
            self.assertIn(self.ADDR, step['body'])
            self.assertIn('Reply "no"', step['body'])

    def test_copy_is_plain_ascii(self):
        """Em-dashes and curly quotes read as machine-written."""
        from outreach import sequences
        for step in sequences.build_steps('texas-law', self.ADDR):
            non_ascii = {c for c in step['body'] + step['subject']
                         if ord(c) > 127}
            self.assertEqual(non_ascii, set())

    def test_machine_punctuation_is_caught(self):
        from outreach import sequences
        bad = [{'subject': 's', 'delay_days': 0,
                'body': 'Hi ' + chr(8212) + ' Aspired Websites LLC'}]
        problems = sequences.describe_problems(bad)
        self.assertTrue(any('em-dash' in p for p in problems))

    def test_html_body_is_caught(self):
        """Business rule 7: cold email is plain text only."""
        from outreach import sequences
        bad = [{'subject': 's', 'delay_days': 0,
                'body': '<p>Hi</p> Aspired Websites LLC'}]
        self.assertTrue(any('HTML' in p
                            for p in sequences.describe_problems(bad)))

    def test_first_touch_must_have_a_subject(self):
        from outreach import sequences
        bad = [{'subject': '', 'delay_days': 0,
                'body': 'Hi. Aspired Websites LLC'}]
        self.assertTrue(any('subject' in p
                            for p in sequences.describe_problems(bad)))

    def test_followups_thread_under_the_first(self):
        """A blank subject threads; a new subject reads as a sequence."""
        from outreach import sequences
        steps = sequences.build_steps('texas-law', self.ADDR)
        self.assertTrue(steps[0]['subject'])
        for step in steps[1:]:
            self.assertEqual(step['subject'], '')

    def test_delays_are_four_touches_over_24_days(self):
        from outreach import sequences
        steps = sequences.build_steps('texas-law', self.ADDR)
        self.assertEqual([s['delay_days'] for s in steps], [0, 3, 7, 14])

    def test_template_makes_no_per_lead_factual_claim(self):
        """Template copy is identical for everyone, so it cannot assert
        anything about ONE lead's site. Only the icebreaker can, because
        only the icebreaker is generated from that lead's measurements.

        Checked across every offer and every touch -- a new offer is
        exactly where someone would reintroduce "your site is slow".
        """
        from outreach import sequences

        forbidden = (
            'your site is not encrypted', "your site isn't encrypted",
            'i checked your', 'your site is slow', 'your site is outdated',
            'your intake form is not', "your intake form isn't",
            'your certificate is', 'your pagespeed score is',
        )
        for offer in sequences.OFFERS:
            for i, step in enumerate(
                    sequences.build_steps('texas-law', self.ADDR,
                                          offer=offer), 1):
                low = step['body'].lower()
                for claim in forbidden:
                    with self.subTest(offer=offer, touch=i, claim=claim):
                        self.assertNotIn(claim, low)

    def test_unknown_sequence_name_is_rejected(self):
        from outreach import sequences
        with self.assertRaises(sequences.SequenceError):
            sequences.build_steps('does-not-exist', self.ADDR)


# ── polling ingest (no webhook plan) ───────────────────────────────────

@override_settings(INSTANTLY_TOKEN='t')
class PollIngestTests(TestCase):
    """Instantly gates webhooks behind a higher plan. GET /emails is not
    gated, so replies arrive by polling. Both paths must share one
    filter -- a filter that applies to only one ingest is not a filter.
    """

    def setUp(self):
        self.lead = make_lead(instantly_lead_id='inst-1')

    def _poll(self, items):
        from outreach import instantly_poll
        with patch.object(instantly, 'list_emails', return_value=items), \
             patch.object(instantly, 'list_accounts', return_value=[
                 {'email': 'zach@getaspiredwebsites.com'}]), \
             patch('outreach.instantly_webhook._pause_quietly'), \
             patch('outreach.tasks.classify_and_draft_reply_task.delay'):
            return instantly_poll.poll_replies()

    def test_inbound_reply_is_ingested(self):
        summary = self._poll([{
            'id': 'msg-1', 'ue_type': 2,
            'from_address_email': self.lead.email,
            'subject': 'Re: quick question',
            'body': {'text': 'Sure, send it over.'},
        }])
        self.assertEqual(summary['replies'], 1)
        self.assertEqual(EmailReply.objects.count(), 1)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'replied')

    def test_outbound_message_is_ignored(self):
        """ue_type 1 is us sending. Ingesting it would file our own copy."""
        summary = self._poll([{
            'id': 'msg-2', 'ue_type': 1,
            'from_address_email': 'zach@getaspiredwebsites.com',
            'to_address_email': self.lead.email,
            'body': {'text': 'Hi Sarah, ...'},
        }])
        self.assertEqual(summary['inbound'], 0)
        self.assertEqual(EmailReply.objects.count(), 0)

    def test_own_domain_is_filtered_on_the_poll_path_too(self):
        """The same bug, via the other door."""
        Lead.objects.create(firm_name='Aspired AI LLC',
                            email='hello@aspired-ai.com', source='manual')
        summary = self._poll([{
            'id': 'msg-3', 'ue_type': 2,
            'from_address_email': 'hello@aspired-ai.com',
            'subject': 'Google Ads budget',
            'body': {'text': 'Progress toward the $1,000 credit...'},
        }])
        self.assertEqual(summary['filtered'], 1)
        self.assertEqual(EmailReply.objects.count(), 0)

    def test_polling_is_idempotent(self):
        item = {'id': 'msg-4', 'ue_type': 2,
                'from_address_email': self.lead.email,
                'body': {'text': 'yes please'}}
        self._poll([item])
        second = self._poll([item])
        self.assertEqual(second['new'], 0)
        self.assertEqual(EmailReply.objects.count(), 1)
        self.assertEqual(InstantlyEvent.objects.count(), 1)

    def test_bounce_notice_suppresses_the_failed_address(self):
        """A bounce arrives as mail from mailer-daemon, not as an event.

        The reply filter would drop it as an automated sender, so it has
        to be classified as a bounce BEFORE the filter sees it.
        """
        summary = self._poll([{
            'id': 'msg-5', 'ue_type': 2,
            'from_address_email': 'mailer-daemon@googlemail.com',
            'subject': 'Delivery Status Notification (Failure)',
            'body': {'text': f'Your message to {self.lead.email} could '
                             f'not be delivered. 550 no such user.'},
        }])
        self.assertEqual(summary['bounces'], 1)
        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.email_verification_status, verify.INVALID)
        self.assertTrue(
            SuppressionList.objects.filter(email=self.lead.email).exists())

    def test_bounce_does_not_suppress_the_daemon_address(self):
        """Taking the first address in the notice would suppress
        postmaster@, our own mailbox, or a support URL."""
        self._poll([{
            'id': 'msg-6', 'ue_type': 2,
            'from_address_email': 'mailer-daemon@googlemail.com',
            'subject': 'Undeliverable',
            'body': {'text': f'postmaster@example.com reports: message to '
                             f'{self.lead.email} failed.'},
        }])
        self.assertFalse(SuppressionList.objects.filter(
            email='postmaster@example.com').exists())
        self.assertTrue(SuppressionList.objects.filter(
            email=self.lead.email).exists())

    def test_bounce_for_an_unknown_address_suppresses_nothing(self):
        summary = self._poll([{
            'id': 'msg-7', 'ue_type': 2,
            'from_address_email': 'mailer-daemon@googlemail.com',
            'subject': 'Undeliverable',
            'body': {'text': 'message to nobody@nowhere.com failed'},
        }])
        self.assertEqual(SuppressionList.objects.count(), 0)
        self.assertEqual(summary['new'], 1)

    def test_html_only_body_still_yields_text(self):
        self._poll([{
            'id': 'msg-8', 'ue_type': 2,
            'from_address_email': self.lead.email,
            'body': {'html': '<p>Sounds <b>good</b>, send it.</p>'},
        }])
        reply = EmailReply.objects.get()
        self.assertIn('Sounds', reply.body)
        self.assertNotIn('<b>', reply.body)

    def test_api_failure_is_reported_not_raised(self):
        from outreach import instantly_poll
        with patch.object(instantly, 'list_emails',
                          side_effect=instantly.InstantlyError('502')):
            summary = instantly_poll.poll_replies()
        self.assertIn('502', summary['error'])
        self.assertEqual(summary['polled'], 0)

    @override_settings(INSTANTLY_TOKEN='')
    def test_missing_token_is_reported_not_raised(self):
        from outreach import instantly_poll
        summary = instantly_poll.poll_replies()
        self.assertIn('INSTANTLY_TOKEN', summary['error'])


# ── site measurement (the false-claim bug) ─────────────────────────────

class SiteClassificationTests(TestCase):
    """has_ssl used to mean "an https GET returned 200", which is not a
    TLS measurement. Verified against two real leads 2026-08-22:

        scientificsearch.com    HTTP 403 (bot-blocked)  -> has_ssl=False
        theascendantgroup.com   HTTP 404 (parked Wix)   -> has_ssl=False

    Both serve valid certificates. The icebreaker then told one of them
    their site "is still running on plain HTTP" -- a false, checkable
    claim about a stranger's business. The guard verified the claim
    against the measurement; nobody verified the measurement.
    """

    def test_bot_blocked_is_not_parked(self):
        """403 means someone is home and will not talk to scrapers."""
        from outreach.enricher import classify_site, ISSUE_BOT_BLOCKED
        self.assertEqual(classify_site(403, ''), ISSUE_BOT_BLOCKED)
        self.assertEqual(classify_site(429, ''), ISSUE_BOT_BLOCKED)

    def test_404_is_parked(self):
        from outreach.enricher import classify_site, ISSUE_PARKED
        self.assertEqual(classify_site(404, ''), ISSUE_PARKED)

    def test_wix_placeholder_is_parked(self):
        from outreach.enricher import classify_site, ISSUE_PARKED
        html = "<html><body><h1>This domain isn't connected to a site</h1>" \
               "<p>If this domain is yours, head to the Domains page.</p>" \
               "</body></html>"
        self.assertEqual(classify_site(200, html), ISSUE_PARKED)

    def test_js_rendered_site_is_live_not_parked(self):
        """THE FALSE POSITIVE. careerpathwayllc.com returns 200 with 2,306
        bytes and nine words -- content is rendered client-side. A naive
        word-count rule flagged it as parked, which would have suppressed
        every real signal for a live business."""
        from outreach.enricher import classify_site
        html = ('<html><head><title>Career Pathway LLC - Elite Staffing '
                '&amp; Workforce Solutions</title></head><body>'
                '<div id="root"></div><script src="/app.js"></script>'
                '</body></html>')
        self.assertEqual(classify_site(200, html), '')

    def test_thin_scriptless_untitled_page_is_parked(self):
        from outreach.enricher import classify_site, ISSUE_PARKED
        self.assertEqual(
            classify_site(200, '<html><head><title>Home</title></head>'
                               '<body><p>Coming soon</p></body></html>'),
            ISSUE_PARKED)

    def test_real_content_is_live(self):
        from outreach.enricher import classify_site
        html = '<html><body>' + ('word ' * 300) + '</body></html>'
        self.assertEqual(classify_site(200, html), '')

    def test_empty_response_is_unreachable(self):
        from outreach.enricher import classify_site, ISSUE_UNREACHABLE
        self.assertEqual(classify_site(0, ''), ISSUE_UNREACHABLE)


class ParkedSiteObservationTests(TestCase):
    """A parked domain is the ABSENCE of a website, so every site-quality
    signal is meaningless against it. PageSpeed scored a Wix placeholder
    89/100 -- an excellent score for a page that does not exist."""

    def test_parked_site_suppresses_all_quality_signals(self):
        from outreach.enricher import ISSUE_PARKED
        lead = make_lead(site_status=ISSUE_PARKED,
                         website_performance_score=89,
                         has_ssl=True, copyright_year=2015)
        keys = [k for k, _ in icebreaker.observations(lead)]
        self.assertEqual(keys, ['no_real_website'])
        self.assertNotIn('slow', keys)
        self.assertNotIn('stale_copyright', keys)

    def test_unreachable_site_suppresses_all_quality_signals(self):
        from outreach.enricher import ISSUE_UNREACHABLE
        lead = make_lead(site_status=ISSUE_UNREACHABLE,
                         website_performance_score=12)
        self.assertEqual(
            [k for k, _ in icebreaker.observations(lead)],
            ['no_real_website'])

    def test_bot_blocked_site_keeps_pagespeed_signal(self):
        """Google's crawler is not blocked, so the score is still real."""
        from outreach.enricher import ISSUE_BOT_BLOCKED
        lead = make_lead(site_status=ISSUE_BOT_BLOCKED,
                         website_performance_score=29, has_ssl=True)
        keys = [k for k, _ in icebreaker.observations(lead)]
        self.assertIn('slow', keys)
        self.assertNotIn('no_real_website', keys)

    def test_valid_tls_never_produces_a_no_ssl_claim(self):
        """The exact false claim that reached generated copy."""
        lead = make_lead(has_ssl=True, site_status='')
        self.assertNotIn(
            'no_ssl', [k for k, _ in icebreaker.observations(lead)])

    def test_tls_failure_reason_is_carried_into_the_observation(self):
        lead = make_lead(has_ssl=False, site_status='',
                         tls_error='certificate not valid (expired)')
        text = dict(icebreaker.observations(lead))['no_ssl']
        self.assertIn('expired', text)


# ── email preview rendering ────────────────────────────────────────────

class RenderPreviewTests(TestCase):
    """Instantly substitutes variables on its own side at send time, so
    the text a prospect actually receives is never visible from Django.
    render_for_lead makes the thing being approved the thing being sent.
    """

    ADDR = '1 Main St, San Antonio, TX 78205'

    def _first_touch(self, lead):
        from outreach import sequences
        steps = sequences.build_steps('texas-law', self.ADDR)
        return sequences.render_for_lead(steps[0], lead)

    def test_all_variables_are_substituted(self):
        from outreach import sequences
        lead = make_lead()
        rendered = self._first_touch(lead)
        self.assertEqual(
            sequences.unresolved_variables(rendered['body']), [])
        self.assertEqual(
            sequences.unresolved_variables(rendered['subject']), [])

    def test_first_name_only_not_full_name(self):
        """"Hi Sarah Chen," reads like a mail merge; "Hi Sarah," does not."""
        lead = make_lead(attorney_name='Sarah Chen')
        body = self._first_touch(lead)['body']
        self.assertIn('Hi Sarah,', body)
        self.assertNotIn('Hi Sarah Chen,', body)

    def test_icebreaker_appears_verbatim(self):
        lead = make_lead(icebreaker='Your site scores 31/100 on PageSpeed.')
        self.assertIn('Your site scores 31/100 on PageSpeed.',
                      self._first_touch(lead)['body'])

    def test_company_name_reaches_the_subject(self):
        lead = make_lead(firm_name='Chen Law Group')
        self.assertIn('Chen Law Group', self._first_touch(lead)['subject'])

    def test_unresolved_variables_are_detected(self):
        """A leftover placeholder ships literally. "Hi {{firstName}}," is
        worse than sending nothing, and it is the classic merge failure."""
        from outreach import sequences
        self.assertEqual(
            sequences.unresolved_variables('Hi {{firstName}}, re {{oops}}'),
            ['firstName', 'oops'])

    def test_missing_contact_name_leaves_no_placeholder(self):
        """Apify rows without a full_name must not ship "Hi {{firstName}},"."""
        from outreach import sequences
        lead = make_lead(attorney_name='')
        rendered = self._first_touch(lead)
        self.assertEqual(
            sequences.unresolved_variables(rendered['body']), [])
        self.assertIn('Hi ,', rendered['body'])

    def test_postal_address_survives_rendering(self):
        self.assertIn(self.ADDR, self._first_touch(make_lead())['body'])


# ── segment matching ───────────────────────────────────────────────────

@override_settings(INSTANTLY_TOKEN='t')
class SegmentGateTests(TestCase):
    """A dry run on 2026-08-23 produced this, two lines apart, in one email:

        "...staffing and recruiting firms in Los Angeles..."  (icebreaker)
        "I build websites for law firms around Texas."        (template)

    Both halves were individually correct and described different
    businesses. No downstream guard could catch it -- only the pairing is
    wrong -- so the pairing is what gets checked.
    """

    def setUp(self):
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law', slug='tx-law', niche='law firm',
            business_type='Law Firm', state='TX',
            instantly_campaign_id='camp-1', active=True)

    def test_the_actual_mismatch_is_refused(self):
        lead = make_lead(firm_name='The Braden James Group',
                         state='California', business_type='Staffing')
        self.assertIn('California', instantly.segment_mismatch(
            lead, self.campaign))

    def test_matching_lead_passes(self):
        lead = make_lead(state='TX', business_type='Law Firm')
        self.assertEqual(instantly.segment_mismatch(lead, self.campaign), '')

    def test_full_state_name_matches_abbreviation(self):
        """Places writes 'Texas', Apify writes 'TX'. Same state."""
        lead = make_lead(state='Texas', business_type='Law Firm')
        self.assertEqual(instantly.segment_mismatch(lead, self.campaign), '')

    def test_wrong_business_type_is_refused(self):
        lead = make_lead(state='TX', business_type='Dentist')
        self.assertIn('Dentist', instantly.segment_mismatch(
            lead, self.campaign))

    def test_blank_campaign_fields_do_not_constrain(self):
        """A deliberately broad campaign must still work."""
        broad = OutreachCampaign.objects.create(
            name='Everything', slug='all', niche='x',
            business_type='', state='',
            instantly_campaign_id='c2', active=True)
        lead = make_lead(state='NJ', business_type='Staffing')
        self.assertEqual(instantly.segment_mismatch(lead, broad), '')

    def test_push_refuses_the_mismatched_lead(self):
        lead = make_lead(state='California', business_type='Staffing')
        with allow_sending(), patch.object(
                instantly, '_request',
                return_value={'id': 'x'}) as req:
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['pushed'], 0)
        self.assertEqual(summary['skipped_wrong_segment'], 1)
        req.assert_not_called()


class EnrichmentQueueTests(TestCase):
    """Enrichment is ~30s and a PageSpeed call per lead. Spending it on an
    address verification has already killed for good buys nothing."""

    def _queued(self):
        from outreach.models import Lead
        from outreach import verify as v
        return set(Lead.objects.filter(
            enrichment_completed_at__isnull=True, unsubscribed=False,
        ).exclude(
            email_verification_status__in=[v.ROLE, v.INVALID],
        ).exclude(website='').values_list('firm_name', flat=True))

    def test_role_and_invalid_are_not_enriched(self):
        make_lead(firm_name='RoleCo', email='info@x.com',
                  website='https://x.com',
                  email_verification_status=verify.ROLE)
        make_lead(firm_name='DeadCo', email='a@y.com',
                  website='https://y.com',
                  email_verification_status=verify.INVALID)
        queued = self._queued()
        self.assertNotIn('RoleCo', queued)
        self.assertNotIn('DeadCo', queued)

    def test_pending_IS_enriched(self):
        """A Places lead with no address yet is PENDING, and enrichment is
        the stage that finds its email. Skipping it would strand exactly
        the leads that need this most."""
        make_lead(firm_name='NoEmailCo', email='',
                  website='https://z.com',
                  email_verification_status=verify.PENDING)
        self.assertIn('NoEmailCo', self._queued())

    def test_valid_and_consumer_are_enriched(self):
        make_lead(firm_name='GoodCo', website='https://g.com',
                  email_verification_status=verify.VALID)
        make_lead(firm_name='GmailCo', email='a@gmail.com',
                  website='https://h.com',
                  email_verification_status=verify.CONSUMER)
        queued = self._queued()
        self.assertIn('GoodCo', queued)
        self.assertIn('GmailCo', queued)


class HttpsFallbackTests(TestCase):
    """A broken certificate does not mean the site is down.

    Verified 2026-08-23: texashealthlawattorney.com fails TLS with a
    hostname mismatch and returns 200 with 3,717 words over http. Without
    a downgrade attempt it classified as 'unreachable' and the generator
    announced that a working law firm's website was offline.

    The true finding is stronger: a live site with no usable HTTPS, for a
    firm handling protected health information.
    """

    def test_https_failure_falls_back_to_http(self):
        from outreach import enricher
        lead = make_lead(website='https://example-firm.com')
        calls = []

        def fake_get(url):
            calls.append(url)
            if url.startswith('https://'):
                return '', url, False, 0
            return '<html>' + ('word ' * 200) + '</html>', url, True, 200

        with patch.object(enricher, '_http_get_status', side_effect=fake_get), \
             patch.object(enricher, 'probe_tls',
                          return_value=(False, 'hostname mismatch')), \
             patch.object(enricher, '_run_pagespeed'), \
             patch.object(enricher, '_extract_from_html'):
            enricher._scrape_homepage(lead)

        self.assertTrue(any(u.startswith('http://') for u in calls),
                        'never attempted the http downgrade')
        self.assertEqual(lead.site_status, '',
                         'a reachable site must not be marked unreachable')
        self.assertFalse(lead.has_ssl)
        self.assertIn('hostname mismatch', lead.tls_error)

    def test_live_site_with_broken_tls_yields_the_ssl_observation(self):
        """Not 'no_real_website' -- the site is up, the certificate is not."""
        lead = make_lead(site_status='', has_ssl=False,
                         tls_error='certificate not valid (Hostname mismatch)')
        keys = [k for k, _ in icebreaker.observations(lead)]
        self.assertIn('no_ssl', keys)
        self.assertNotIn('no_real_website', keys)


# ── manual review queue ────────────────────────────────────────────────

class ReviewFlagTests(TestCase):
    """Apollo mis-tags. Verified 2026-08-23, all four came back as
    industry='Legal Services':

        Bwa Video, Inc.                      title='Owner'
        Kinney Recruiting                    title='Co-owner'
        Patent Designs                       title='Owner'
        National Employment Lawyers Assoc.   title='Owner'

    No actor-side filter can exclude a recruiting company the source
    calls a law practice -- a tightened run returned 100 identical rows
    and excluded none of them. So the check runs here, on the one field
    the source cannot mislabel: the company's own name.
    """

    def _flagged(self, name):
        from outreach import review
        return bool(review.describe_review_reasons(make_lead(firm_name=name)))

    def test_the_four_real_offenders_are_flagged(self):
        for name in ('Bwa Video, Inc.', 'Kinney Recruiting',
                     'Patent Designs',
                     'National Employment Lawyers Association'):
            with self.subTest(name=name):
                self.assertTrue(self._flagged(name))

    def test_real_law_firms_are_not_flagged(self):
        """False positives are the expensive failure -- these are all real
        firms pulled from the live dataset."""
        for name in ('Chalker Flores, Llp', 'The Stevenson Law Firm, Pc',
                     'Davidson Law Group', 'Powers Taylor Llp',
                     'Hill Law Firm Accident And Injury Lawyers',
                     'Law Office Of Mark A. Ticer',
                     'Givens & Johnston Pllc', 'The Monsour Law Firm Pc'):
            with self.subTest(name=name):
                self.assertFalse(self._flagged(name))

    def test_law_wording_overrides_a_weak_marker(self):
        """"Legal Solutions PLLC" is a real firm, not a software company."""
        self.assertFalse(self._flagged('Legal Design Solutions PLLC'))
        self.assertFalse(self._flagged('Law Offices of Media & Entertainment'))

    def test_association_is_flagged_even_with_law_wording(self):
        """A bar association is not a practice, however legal its name."""
        self.assertTrue(self._flagged('Texas Trial Lawyers Association'))

    def test_matching_is_word_level_not_substring(self):
        """'design' inside 'designated', 'pc' inside 'pacific'."""
        self.assertFalse(self._flagged('Designated Counsel PC'))
        self.assertFalse(self._flagged('Pacific Law Partners'))

    def test_flag_lead_persists_and_clears(self):
        from outreach import review
        lead = make_lead(firm_name='Kinney Recruiting')
        self.assertTrue(review.flag_lead(lead))
        lead.refresh_from_db()
        self.assertTrue(lead.needs_review)
        self.assertIn('recruiting', lead.review_reason.lower())

        lead.firm_name = 'Kinney Law Firm'
        self.assertFalse(review.flag_lead(lead))
        lead.refresh_from_db()
        self.assertFalse(lead.needs_review)


@override_settings(INSTANTLY_TOKEN='t')
class ReviewGateTests(TestCase):
    """Flagged leads are HELD, not dropped. Clearing one releases it."""

    def setUp(self):
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law', slug='tx-law-rev', niche='law firm',
            instantly_campaign_id='c1', active=True)

    def test_flagged_lead_is_not_pushed(self):
        lead = make_lead(needs_review=True,
                         review_reason='Name contains "recruiting"')
        with allow_sending(), patch.object(
                instantly, '_request',
                return_value={'id': 'x'}) as req:
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['pushed'], 0)
        self.assertEqual(summary['skipped_needs_review'], 1)
        req.assert_not_called()

    def test_cleared_lead_pushes_normally(self):
        lead = make_lead(needs_review=False)
        with allow_sending(), patch.object(
                instantly, '_request',
                return_value={'id': 'x'}):
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['pushed'], 1)


class OfferCompositionTests(TestCase):
    """Six offers, one template. The offer is the A/B variable."""

    ADDR = '1 Main St, San Antonio, TX 78205'

    def test_every_offer_builds_and_preflights_clean(self):
        from outreach import sequences
        self.assertEqual(len(sequences.OFFERS), 6)
        for key in sequences.OFFERS:
            with self.subTest(offer=key):
                steps = sequences.build_steps(
                    'texas-law', self.ADDR, offer=key)
                self.assertEqual(sequences.describe_problems(steps), [])

    def test_composing_the_offer_does_not_eat_instantly_variables(self):
        """THE BUG. str.format() reads '{{' as an escape for a literal
        '{', so composing the offer rewrote every {{firstName}} to
        {firstName} -- which ships to the prospect as "Hi {firstName},".
        Invisible in every funnel count."""
        from outreach import sequences
        for key in sequences.OFFERS:
            with self.subTest(offer=key):
                body = sequences.build_steps(
                    'texas-law', self.ADDR, offer=key)[0]['body']
                self.assertIn('{{firstName}}', body)
                self.assertIn('{{icebreaker}}', body)
                self.assertNotIn('{firstName}', body.replace('{{firstName}}', ''))

    def test_no_unsubstituted_offer_slot_survives(self):
        from outreach import sequences
        for key in sequences.OFFERS:
            for step in sequences.build_steps('texas-law', self.ADDR,
                                              offer=key):
                self.assertNotIn('{offer_', step['body'])
                self.assertNotIn('{postal}', step['body'])

    def test_offers_actually_differ(self):
        """Six identical arms would be six identical numbers."""
        from outreach import sequences
        bodies = {
            k: sequences.build_steps('texas-law', self.ADDR, offer=k)[0]['body']
            for k in sequences.OFFERS
        }
        self.assertEqual(len(set(bodies.values())), 6)

    def test_unknown_offer_is_rejected(self):
        from outreach import sequences
        with self.assertRaises(sequences.SequenceError):
            sequences.build_steps('texas-law', self.ADDR, offer='nope')

    def test_every_offer_declares_its_fulfilment_cost(self):
        """An offer with a great reply rate that costs four hours to
        fulfil is a trap: succeed and you have sold yourself into unpaid
        full-time work."""
        from outreach import sequences
        for key, spec in sequences.OFFERS.items():
            with self.subTest(offer=key):
                self.assertTrue(spec['fulfilment_cost'].strip())
                self.assertTrue(spec['appeals_to'].strip())


# ── offers as rows ─────────────────────────────────────────────────────

class OfferModelTests(TestCase):
    """Offers moved from a dict in code to rows in the database.

    A constant means changing an offer needs a deploy, which is wrong for
    the same reason hardcoded prices are wrong, and makes the point of
    measuring offers unreachable: an agent that learns which offer wins
    but cannot act on it has learned nothing useful.
    """

    def _offer(self, **kw):
        from outreach.models import Offer
        defaults = {
            'key': 'db_offer', 'name': 'DB offer',
            'pitch': 'I will do the thing for free, no strings.',
            'restate': 'the thing, free',
            'ask': 'Reply "yes".',
        }
        defaults.update(kw)
        return Offer.objects.create(**defaults)

    def test_database_row_beats_the_code_constant(self):
        """Same key in both -> the row wins, because the row is the one a
        human can edit without a deploy."""
        from outreach import sequences
        self._offer(key='security_review',
                    pitch='COMPLETELY DIFFERENT PITCH, free.',
                    restate='a different thing, free',
                    ask='Reply "sure".')
        body = sequences.build_steps(
            'texas-law', '1 Main St', offer='security_review')[0]['body']
        self.assertIn('COMPLETELY DIFFERENT PITCH', body)
        self.assertNotIn('within 48 hours', body)

    def test_code_constant_is_the_fallback(self):
        """Keeps the module usable on a fresh checkout before seeding."""
        from outreach import sequences
        body = sequences.build_steps(
            'texas-law', '1 Main St', offer='security_review')[0]['body']
        self.assertIn('security and performance review', body)

    def test_an_offer_row_can_be_passed_directly(self):
        from outreach import sequences
        offer = self._offer()
        body = sequences.build_steps(
            'texas-law', '1 Main St', offer=offer)[0]['body']
        self.assertIn('I will do the thing for free', body)

    def test_new_offers_are_inactive_by_default(self):
        """An agent-proposed offer is a proposal, never something that
        starts going out on its own."""
        self.assertFalse(self._offer().active)

    def test_reply_rate_is_zero_before_any_sends(self):
        offer = self._offer()
        self.assertEqual(offer.reply_rate, 0.0)
        offer.sends, offer.replies = 200, 8
        self.assertAlmostEqual(offer.reply_rate, 0.04)

    def test_unknown_key_names_where_to_fix_it(self):
        from outreach import sequences
        with self.assertRaises(sequences.SequenceError) as ctx:
            sequences.build_steps('texas-law', '1 Main St', offer='nope')
        self.assertIn('seed_offers', str(ctx.exception))


class ComposeEmailTests(TestCase):
    """compose_email is THE draft function. Both per-thing variables come
    from different places and both are required:

        offer      <- the campaign (the A/B arm)
        icebreaker <- the lead     (that lead's own facts)
    """

    def setUp(self):
        from outreach.models import Offer
        self.offer = Offer.objects.create(
            key='speed_test', name='Speed', active=True,
            pitch='I will make your site fast, or you pay nothing.',
            restate='your site made fast, or you pay nothing',
            ask='Reply "yes".')
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law', slug='tx-compose', niche='law firm',
            offer=self.offer, instantly_campaign_id='c1', active=True)

    def test_uses_both_the_campaign_offer_and_the_lead_icebreaker(self):
        from outreach import sequences
        lead = make_lead(icebreaker='Twenty years in probate is unusual.')
        d = sequences.compose_email(lead, campaign=self.campaign,
                                    postal_address='1 Main St')
        self.assertIn('Twenty years in probate is unusual.', d['body'])
        self.assertIn('I will make your site fast', d['body'])
        self.assertEqual(d['offer_key'], 'speed_test')
        self.assertTrue(d['has_icebreaker'])
        self.assertEqual(d['unresolved'], [])

    def test_explicit_offer_overrides_the_campaign(self):
        """Lets a caller preview an alternative without touching a live
        campaign."""
        from outreach import sequences
        d = sequences.compose_email(
            make_lead(), campaign=self.campaign, offer='security_review',
            postal_address='1 Main St')
        self.assertEqual(d['offer_key'], 'security_review')
        self.assertNotIn('I will make your site fast', d['body'])

    def test_missing_icebreaker_is_reported_not_hidden(self):
        from outreach import sequences
        d = sequences.compose_email(
            make_lead(icebreaker=''), campaign=self.campaign,
            postal_address='1 Main St')
        self.assertFalse(d['has_icebreaker'])

    def test_every_touch_composes_cleanly(self):
        from outreach import sequences
        lead = make_lead()
        for touch in (1, 2, 3, 4):
            with self.subTest(touch=touch):
                d = sequences.compose_email(
                    lead, campaign=self.campaign, touch=touch,
                    postal_address='1 Main St')
                self.assertEqual(d['unresolved'], [])
                self.assertEqual(d['touch'], touch)

    def test_campaign_without_an_offer_falls_back_to_default(self):
        from outreach import sequences
        bare = OutreachCampaign.objects.create(
            name='Bare', slug='bare', niche='x')
        d = sequences.compose_email(make_lead(), campaign=bare,
                                    postal_address='1 Main St')
        self.assertEqual(d['offer_key'], sequences.DEFAULT_OFFER)
        self.assertEqual(d['unresolved'], [])


# ── the send gates ─────────────────────────────────────────────────────

@override_settings(INSTANTLY_TOKEN='t')
class SendGateWarmupTests(TestCase):
    """Two independent gates. A switch alone is one mis-click away from
    270 emails/day out of mailboxes that finished setup yesterday, and
    providers do not care that a human ticked a box."""

    def setUp(self):
        from outreach.models import OutreachSettings
        self.cfg = OutreachSettings.load()
        self.cfg.instantly_sending_enabled = True
        self.cfg.min_warmup_score = 90
        self.cfg.min_warmup_days = 14
        self.cfg.min_ready_mailboxes = 3
        self.cfg.save()

    def _accounts(self, n=3, score=100, days_ago=30,
                  pending=False, status=1):
        from datetime import datetime, timedelta, timezone as tz
        start = (datetime.now(tz.utc) - timedelta(days=days_ago)
                 ).isoformat().replace('+00:00', 'Z')
        return [{
            'email': f'zach{i}@getaspiredwebsites.com',
            'stat_warmup_score': score,
            'timestamp_warmup_start': None if days_ago is None else start,
            'setup_pending': pending,
            'status': status,
            'daily_limit': 30,
        } for i in range(n)]

    def test_switch_off_blocks_even_when_mailboxes_are_perfect(self):
        self.cfg.instantly_sending_enabled = False
        self.cfg.save()
        with patch.object(instantly, 'list_accounts',
                          return_value=self._accounts()):
            allowed, why = instantly.sending_allowed()
        self.assertFalse(allowed)
        self.assertIn('switched off', why)

    def test_switch_on_but_mailboxes_unwarmed_still_blocks(self):
        """THE POINT. The switch cannot override the measurement."""
        with patch.object(instantly, 'list_accounts',
                          return_value=self._accounts(pending=True,
                                                      status=2)):
            allowed, why = instantly.sending_allowed()
        self.assertFalse(allowed)
        self.assertIn('not warm enough', why)

    def test_the_real_aspiredwebsites_mailboxes_fail_today(self):
        """Verified live 2026-08-23: status=2, setup_pending=True,
        warmup_start=None. They have never been connected."""
        live = [{'email': 'zach@getaspiredwebsites.com',
                 'stat_warmup_score': 0, 'timestamp_warmup_start': None,
                 'setup_pending': True, 'status': 2, 'daily_limit': 30}] * 9
        with patch.object(instantly, 'list_accounts', return_value=live):
            status = instantly.warmup_readiness()
        self.assertFalse(status['ready'])
        self.assertEqual(status['ready_mailboxes'], 0)

    def test_warm_enough_and_switched_on_allows(self):
        with patch.object(instantly, 'list_accounts',
                          return_value=self._accounts()):
            allowed, why = instantly.sending_allowed()
        self.assertTrue(allowed)
        self.assertEqual(why, '')

    def test_good_score_but_too_new_is_refused(self):
        """A brand new mailbox can post a flattering score days before
        any provider trusts it."""
        with patch.object(instantly, 'list_accounts',
                          return_value=self._accounts(score=100,
                                                      days_ago=3)):
            self.assertFalse(instantly.warmup_readiness()['ready'])

    def test_threshold_is_lowerable_for_a_deliberate_early_start(self):
        """14 days is a floor, not a rule. Lowering it is a decision
        about the sending domains, and the admin allows it."""
        self.cfg.min_warmup_days = 7
        self.cfg.save()
        with patch.object(instantly, 'list_accounts',
                          return_value=self._accounts(days_ago=8)):
            self.assertTrue(instantly.warmup_readiness()['ready'])

    def test_too_few_ready_mailboxes_is_refused(self):
        """Rotation needs more than one mailbox."""
        with patch.object(instantly, 'list_accounts',
                          return_value=self._accounts(n=2)):
            self.assertFalse(instantly.warmup_readiness()['ready'])

    def test_push_leads_refuses_when_gated(self):
        self.cfg.instantly_sending_enabled = False
        self.cfg.save()
        campaign = OutreachCampaign.objects.create(
            name='TX', slug='tx-gate', niche='law firm',
            instantly_campaign_id='c1', active=True)
        with patch.object(instantly, '_request') as req:
            with self.assertRaises(instantly.InstantlySendingDisabled):
                instantly.push_leads([make_lead()], campaign)
        req.assert_not_called()

    def test_api_outage_fails_closed(self):
        with patch.object(instantly, 'list_accounts',
                          side_effect=instantly.InstantlyError('502')):
            status = instantly.warmup_readiness()
        self.assertFalse(status['ready'])
        self.assertIn('502', status['reason'])


# ── inbound leads never get cold outreach ──────────────────────────────

@override_settings(INSTANTLY_TOKEN='t')
class InboundLeadTests(TestCase):
    """People who contacted US must never receive a cold sequence.

    An inbound lead used to clear verification, the segment gate and the
    icebreaker guard identically to a scraped one. The only thing between
    a contact-form submission and a cold email opening "I've been
    reaching out to law firms in Houston and yours caught my eye" was
    that campaign assignment had not been built yet. That is luck, not a
    safeguard.
    """

    def setUp(self):
        self.campaign = OutreachCampaign.objects.create(
            name='TX Law', slug='tx-inbound', niche='law firm',
            instantly_campaign_id='c1', active=True)

    def test_contact_form_lead_is_not_pushed(self):
        lead = make_lead(source='contact_form')
        with allow_sending(), patch.object(
                instantly, '_request', return_value={'id': 'x'}) as req:
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['pushed'], 0)
        self.assertEqual(summary['skipped_inbound'], 1)
        req.assert_not_called()

    def test_audit_tool_lead_is_not_pushed(self):
        lead = make_lead(source='audit_tool')
        with allow_sending(), patch.object(
                instantly, '_request', return_value={'id': 'x'}) as req:
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['skipped_inbound'], 1)
        req.assert_not_called()

    def test_scraped_lead_is_still_pushed(self):
        """The guard must not quietly block everything."""
        lead = make_lead(source='apify')
        with allow_sending(), patch.object(
                instantly, '_request', return_value={'id': 'x'}):
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['pushed'], 1)

    def test_inbound_beats_every_other_eligibility_check(self):
        """A perfectly eligible inbound lead is still refused - source
        outranks verification, segment and everything else."""
        lead = make_lead(source='contact_form', state='TX',
                         business_type='Law Firm',
                         email_verification_status=verify.VALID)
        self.campaign.state = 'TX'
        self.campaign.business_type = 'Law Firm'
        self.campaign.save()
        self.assertEqual(instantly.segment_mismatch(lead, self.campaign), '')
        with allow_sending(), patch.object(
                instantly, '_request', return_value={'id': 'x'}) as req:
            summary = instantly.push_leads([lead], self.campaign)
        self.assertEqual(summary['skipped_inbound'], 1)
        req.assert_not_called()

    def test_is_inbound_property(self):
        self.assertTrue(make_lead(source='contact_form').is_inbound)
        self.assertTrue(make_lead(source='audit_tool').is_inbound)
        self.assertFalse(make_lead(source='apify').is_inbound)
        self.assertFalse(make_lead(source='google_maps').is_inbound)

    def test_no_claude_spend_on_inbound_copy(self):
        """Cold copy for someone who already wrote to us is money spent
        on a thing that must never be sent."""
        from django.utils import timezone
        from outreach.tasks import generate_icebreakers_task

        make_lead(source='contact_form', icebreaker='',
                  enrichment_completed_at=timezone.now())
        with patch('outreach.icebreaker.generate') as gen:
            generate_icebreakers_task(limit=10)
        gen.assert_not_called()
