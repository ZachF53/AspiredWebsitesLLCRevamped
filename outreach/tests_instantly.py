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
        with patch.object(instantly, '_request',
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
        with self.assertRaises(instantly.InstantlyError):
            instantly.push_leads([make_lead()], self.campaign)

    def test_campaign_without_instantly_id_refuses_the_push(self):
        self.campaign.instantly_campaign_id = ''
        self.campaign.save()
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

    def test_no_postal_address_refuses_to_build(self):
        """CAN-SPAM requires it; forgetting must be impossible, not likely."""
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
        """Touch 3 must OFFER to check SSL, never report a result.

        A template cannot know whether this lead's site has SSL, so any
        assertion about it is false for everyone who does.
        """
        from outreach import sequences
        body = sequences.build_steps('texas-law', self.ADDR)[2]['body']
        for claim in ('your site is not encrypted',
                      "isn't encrypted",
                      'I checked your'):
            self.assertNotIn(claim.lower(), body.lower())
        self.assertIn('Want me to run it', body)

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
