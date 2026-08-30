"""
Niche verification from the company's own homepage.

The rule under test above all others: a failed fetch is NOT evidence of
being off-niche. A third of real homepages cannot be read — measured
8/12 on live law-firm sites — so treating a timeout as "not in the
niche" would silently delete a third of every batch.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from outreach import site_check
from outreach.models import Lead


def _lead(**kw):
    defaults = {
        'firm_name': 'Barrett Law Group', 'email': 'a@barrettlaw.com',
        'website': 'https://barrettlaw.com', 'business_type': 'Law Firm',
        'city': 'Austin', 'state': 'Texas', 'source': 'apify',
    }
    defaults.update(kw)
    return Lead.objects.create(**defaults)


class BoilerplateStripTests(TestCase):

    def test_the_generated_sentence_goes_and_real_copy_stays(self):
        """The whole field used to be discarded, which threw away the
        records where the business's own writing follows the filler."""
        text = ('Barrett Law Group is a law company based out of 123 Main '
                'St, Austin, TX. We have represented injured Texans for '
                'thirty years and try every case ourselves.')
        out = site_check.strip_provider_boilerplate(text)
        self.assertNotIn('123 Main St', out)
        self.assertNotIn('based out of', out)
        self.assertIn('thirty years', out)

    def test_filler_only_becomes_empty(self):
        out = site_check.strip_provider_boilerplate(
            'Acme LLC is an accounting company based out of 9 Elm Road, '
            'Houston, Texas.')
        self.assertEqual(out, '')

    def test_real_copy_alone_is_untouched(self):
        text = 'We handle probate and estate planning for Texas families.'
        self.assertEqual(site_check.strip_provider_boilerplate(text), text)


class PageTextTests(TestCase):

    def test_title_meta_and_body_are_all_captured(self):
        html = ('<html><head><title>Barrett Law</title>'
                '<meta name="description" content="Estate planning">'
                '</head><body><script>var x=1;</script>'
                '<p>We handle probate.</p></body></html>')
        text = site_check.extract_page_text(html)
        self.assertIn('Barrett Law', text)
        self.assertIn('Estate planning', text)
        self.assertIn('We handle probate', text)
        self.assertNotIn('var x', text)

    def test_parked_pages_yield_nothing(self):
        for marker in ('This domain is for sale', 'Coming soon',
                       'Welcome to nginx!', 'Account Suspended'):
            html = f'<html><body>{marker}</body></html>'
            self.assertEqual(site_check.extract_page_text(html), '',
                             f'{marker!r} should read as parked')

    def test_text_is_truncated(self):
        html = '<html><body>' + ('word ' * 2000) + '</body></html>'
        self.assertLessEqual(len(site_check.extract_page_text(html)),
                             site_check.MAX_PAGE_CHARS)


class NameFallbackTests(TestCase):

    def test_the_name_carries_a_dead_site(self):
        """"Barrett CPA LLC" with a dead site is still obviously an
        accounting firm."""
        lead = _lead(firm_name='Barrett CPA LLC', business_type='Accounting',
                     website='https://barrettcpa.com')
        self.assertEqual(site_check.name_signal(lead, 'Accounting'), 'cpa')

    def test_the_domain_carries_it_too(self):
        lead = _lead(firm_name='Barrett & Associates',
                     website='https://barrettlawgroup.com')
        self.assertTrue(site_check.name_signal(lead, 'Law Firm'))

    def test_a_short_signal_at_the_end_of_the_label_counts(self):
        lead = _lead(firm_name='Smith & Co', website='https://smithlaw.com')
        self.assertEqual(site_check.name_signal(lead, 'Law Firm'), 'law')

    def test_a_short_signal_buried_in_another_word_does_not(self):
        """lawncare.com is not a law firm, and a wrong CONFIRMATION is
        the direction that costs sending reputation."""
        for domain in ('https://lawncare.com', 'https://lawsonhvac.com',
                       'https://flawlessdetailing.com'):
            lead = _lead(firm_name='Acme Services', website=domain)
            self.assertEqual(
                site_check.name_signal(lead, 'Law Firm'), '', domain)

    def test_a_neutral_name_signals_nothing(self):
        lead = _lead(firm_name='Barrett & Associates',
                     website='https://barrettassoc.com')
        self.assertEqual(site_check.name_signal(lead, 'Law Firm'), '')

    def test_an_unmapped_business_type_signals_nothing(self):
        lead = _lead(business_type='Yoga Studio')
        self.assertEqual(site_check.name_signal(lead, 'Yoga Studio'), '')


class VerdictTests(TestCase):
    """Fail closed, but never on infrastructure grounds."""

    def test_a_readable_page_is_classified_and_summarised(self):
        lead = _lead()
        with patch.object(site_check, 'fetch_many', return_value={
                lead.pk: ('We handle probate and estate planning.', '')}), \
                patch.object(site_check, 'classify_batch', return_value={
                    str(lead.pk): {'verdict': 'confirmed',
                                   'summary': 'probate; estate planning'}}):
            counts = site_check.check_leads([lead])
        lead.refresh_from_db()
        self.assertEqual(counts['confirmed'], 1)
        self.assertEqual(lead.niche_verdict, Lead.NICHE_CONFIRMED)
        self.assertEqual(lead.site_summary, 'probate; estate planning')
        self.assertFalse(lead.needs_review)

    def test_a_dead_site_falls_back_to_the_name_and_is_kept(self):
        """THE rule. A timeout is not evidence of being off-niche, and
        treating it as such silently deletes a third of every batch."""
        lead = _lead(firm_name='Barrett Law Group')
        with patch.object(site_check, 'fetch_many', return_value={
                lead.pk: ('', 'fetch failed: ConnectTimeout')}):
            counts = site_check.check_leads([lead])
        lead.refresh_from_db()
        self.assertEqual(counts['confirmed'], 1)
        self.assertEqual(lead.niche_verdict, Lead.NICHE_CONFIRMED)
        self.assertIn('name/domain says', lead.niche_evidence)
        self.assertFalse(lead.needs_review)

    def test_dead_site_and_neutral_name_is_unconfirmed_not_rejected(self):
        lead = _lead(firm_name='Barrett & Associates',
                     website='https://barrettassoc.com')
        with patch.object(site_check, 'fetch_many', return_value={
                lead.pk: ('', 'fetch failed: ConnectTimeout')}):
            counts = site_check.check_leads([lead])
        lead.refresh_from_db()
        self.assertEqual(counts['unconfirmed'], 1)
        self.assertEqual(lead.niche_verdict, Lead.NICHE_UNCONFIRMED)
        # Held, not deleted.
        self.assertTrue(lead.needs_review)
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())

    def test_a_rejected_lead_is_held_for_review(self):
        lead = _lead()
        with patch.object(site_check, 'fetch_many', return_value={
                lead.pk: ('We fabricate structural steel.', '')}), \
                patch.object(site_check, 'classify_batch', return_value={
                    str(lead.pk): {'verdict': 'rejected', 'summary': ''}}):
            site_check.check_leads([lead])
        lead.refresh_from_db()
        self.assertEqual(lead.niche_verdict, Lead.NICHE_REJECTED)
        self.assertTrue(lead.needs_review)
        self.assertIn('rejected', lead.review_reason)

    def test_a_failed_classification_holds_rather_than_guesses(self):
        lead = _lead()
        with patch.object(site_check, 'fetch_many', return_value={
                lead.pk: ('Some real page text about the firm.', '')}), \
                patch.object(site_check, 'classify_batch', return_value={}):
            site_check.check_leads([lead])
        lead.refresh_from_db()
        self.assertEqual(lead.niche_verdict, Lead.NICHE_UNCONFIRMED)
        self.assertTrue(lead.needs_review)

    def test_rerunning_costs_nothing(self):
        """A crashed run must resume, not re-fetch and re-pay."""
        lead = _lead(niche_verdict=Lead.NICHE_CONFIRMED)
        with patch.object(site_check, 'fetch_many') as fetch, \
                patch.object(site_check, 'classify_batch') as classify:
            counts = site_check.check_leads([lead])
        fetch.assert_not_called()
        classify.assert_not_called()
        self.assertEqual(counts['checked'], 0)


class ClassifierContractTests(TestCase):

    def _reply(self, body):
        return patch('reporting.ai.claude_complete', return_value=body)

    def test_an_unknown_verdict_is_never_a_confirmation(self):
        with self._reply('[{"id": "1", "verdict": "probably", '
                         '"summary": "x"}]'):
            out = site_check.classify_batch(
                [{'id': '1', 'name': 'A', 'text': 't'}], 'Law Firm')
        self.assertEqual(out['1']['verdict'], 'unconfirmed')

    def test_the_summary_is_capped(self):
        long = ' '.join(f'w{i}' for i in range(200))
        with self._reply(f'[{{"id": "1", "verdict": "confirmed", '
                         f'"summary": "{long}"}}]'):
            out = site_check.classify_batch(
                [{'id': '1', 'name': 'A', 'text': 't'}], 'Law Firm')
        self.assertLessEqual(len(out['1']['summary'].split()),
                             site_check.SUMMARY_WORD_CAP)

    def test_unparseable_output_confirms_nothing(self):
        with self._reply('I could not read those pages, sorry.'):
            self.assertEqual(site_check.classify_batch(
                [{'id': '1', 'name': 'A', 'text': 't'}], 'Law Firm'), {})

    def test_an_api_failure_confirms_nothing(self):
        with patch('reporting.ai.claude_complete',
                   side_effect=RuntimeError('rate limited')):
            self.assertEqual(site_check.classify_batch(
                [{'id': '1', 'name': 'A', 'text': 't'}], 'Law Firm'), {})

    def test_the_prompt_forbids_guessing_from_the_name(self):
        self.assertIn('NEVER guess from the company name',
                      site_check.SYSTEM_PROMPT)
        self.assertIn('BLANK summary is correct', site_check.SYSTEM_PROMPT)

    def test_the_prompt_asks_for_extraction_not_selection(self):
        """Selection is the writer model's job — it is the judgement a
        small model is worst at."""
        self.assertIn('Do NOT pick the single most interesting fact',
                      site_check.SYSTEM_PROMPT)


class PipelineGateTests(TestCase):

    def test_icebreakers_are_only_written_for_confirmed_leads(self):
        from django.utils import timezone

        from outreach.tasks import generate_icebreakers_task
        for verdict in (Lead.NICHE_REJECTED, Lead.NICHE_UNCONFIRMED):
            Lead.objects.create(
                firm_name=f'Held {verdict}', email=f'{verdict}@x.com',
                business_type='Law Firm', source='apify',
                niche_verdict=verdict,
                enrichment_completed_at=timezone.now())
        with patch('outreach.icebreaker.generate') as gen:
            generate_icebreakers_task()
        gen.assert_not_called()

    def test_verification_skips_leads_the_homepage_ruled_out(self):
        from outreach.tasks import verify_leads_task
        Lead.objects.create(
            firm_name='Steel Co', email='steel@x.com',
            business_type='Law Firm', source='apify',
            niche_verdict=Lead.NICHE_REJECTED)
        with patch('outreach.verify.verify_lead') as verify:
            verify_leads_task()
        verify.assert_not_called()


class IcebreakerUsesSiteCopyTests(TestCase):

    def test_the_summary_is_offered_as_a_fact(self):
        from outreach import icebreaker
        lead = _lead(site_summary='probate; estate planning; since 1998')
        keys = {k for k, _ in icebreaker.warm_facts(lead)}
        self.assertIn('site_summary', keys)

    def test_a_year_stated_on_their_own_site_is_not_fabrication(self):
        """Without this, "serving Austin since 1998" read straight off
        their homepage is rejected as invented — the opposite of what the
        guard is for."""
        from outreach import icebreaker
        lead = _lead(site_summary='serving Austin families since 1998')
        problems = icebreaker.describe_problems(
            'I saw you have been serving Austin families since 1998.', lead)
        self.assertFalse([p for p in problems if 'never recorded' in p])

    def test_a_year_from_nowhere_is_still_fabrication(self):
        from outreach import icebreaker
        lead = _lead(site_summary='probate and estate planning')
        problems = icebreaker.describe_problems(
            'Practising since 1998, impressive.', lead)
        self.assertTrue([p for p in problems if 'never recorded' in p])
