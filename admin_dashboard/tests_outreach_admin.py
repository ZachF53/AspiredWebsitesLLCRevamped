"""
The outreach management pages — offers, campaigns, review queue.

These exist so the outreach system can be run without opening /admin/.
The tests below cover the things a CRUD page gets wrong: destructive
GETs, deletes that orphan data that cost money, and forms that happily
save a state the rest of the system will refuse.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from outreach.models import Lead, Offer, OutreachCampaign


class OutreachAdminPagesTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_superuser(
            'zach', 'z@example.com', 'pw')
        self.client.force_login(self.user)
        self.offer = Offer.objects.create(
            key='sec', name='Security review', active=True,
            pitch='I will review your site, free.',
            restate='a free review', ask='Reply yes.')

    # ── reachability ───────────────────────────────────────────────────

    def test_every_page_renders(self):
        for name, args in (
            ('outreach_index', []),
            ('outreach_offer_list', []),
            ('outreach_offer_new', []),
            ('outreach_offer_edit', [self.offer.pk]),
            ('outreach_campaign_list', []),
            ('outreach_campaign_new', []),
            ('outreach_review_queue', []),
        ):
            with self.subTest(page=name):
                url = reverse(f'admin_dashboard:{name}', args=args)
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_index_exists(self):
        """/admin-dashboard/outreach/ used to 404 - it was a URL prefix
        with nothing served at it, so the obvious place to navigate to
        was the one place that broke."""
        resp = self.client.get(reverse('admin_dashboard:outreach_index'))
        self.assertEqual(resp.status_code, 200)

    def test_pages_require_login(self):
        self.client.logout()
        url = reverse('admin_dashboard:outreach_offer_list')
        self.assertNotEqual(self.client.get(url).status_code, 200)

    # ── offers ─────────────────────────────────────────────────────────

    def test_create_offer(self):
        resp = self.client.post(
            reverse('admin_dashboard:outreach_offer_new'),
            {'name': 'Speed guarantee', 'pitch': 'Fast or free.',
             'restate': 'your site made fast', 'ask': 'Reply yes.',
             'appeals_to': 'a number they can check'})
        self.assertEqual(resp.status_code, 302)
        offer = Offer.objects.get(key='speed_guarantee')
        self.assertEqual(offer.name, 'Speed guarantee')

    def test_new_offer_is_inactive_unless_ticked(self):
        """An offer that goes live merely by being created is how untested
        copy reaches a stranger."""
        self.client.post(
            reverse('admin_dashboard:outreach_offer_new'),
            {'name': 'Quiet', 'pitch': 'p', 'restate': 'r', 'ask': 'a'})
        self.assertFalse(Offer.objects.get(key='quiet').active)

    def test_offer_requires_a_pitch_and_an_ask(self):
        """Without a pitch there is no offer; without an ask there is no
        way to accept it."""
        resp = self.client.post(
            reverse('admin_dashboard:outreach_offer_new'), {'name': 'Empty'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Offer.objects.filter(key='empty').exists())

    def test_duplicate_offer_key_is_refused(self):
        resp = self.client.post(
            reverse('admin_dashboard:outreach_offer_new'),
            {'name': 'Other', 'key': 'sec', 'pitch': 'p',
             'restate': 'r', 'ask': 'a'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Offer.objects.filter(key='sec').count(), 1)

    def test_offer_delete_confirms_on_get(self):
        """A GET must never destroy anything - a prefetching browser or a
        pasted link will eventually issue one."""
        url = reverse('admin_dashboard:outreach_offer_delete',
                      args=[self.offer.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(Offer.objects.filter(pk=self.offer.pk).exists())

    def test_offer_delete_acts_on_post(self):
        url = reverse('admin_dashboard:outreach_offer_delete',
                      args=[self.offer.pk])
        self.client.post(url)
        self.assertFalse(Offer.objects.filter(pk=self.offer.pk).exists())

    def test_offer_in_use_is_not_deleted(self):
        OutreachCampaign.objects.create(
            name='TX', slug='tx', niche='law firm', offer=self.offer)
        self.client.post(reverse('admin_dashboard:outreach_offer_delete',
                                 args=[self.offer.pk]))
        self.assertTrue(Offer.objects.filter(pk=self.offer.pk).exists())

    def test_offer_toggle(self):
        self.client.post(reverse('admin_dashboard:outreach_offer_toggle',
                                 args=[self.offer.pk]))
        self.offer.refresh_from_db()
        self.assertFalse(self.offer.active)

    # ── campaigns ──────────────────────────────────────────────────────

    def test_create_campaign(self):
        resp = self.client.post(
            reverse('admin_dashboard:outreach_campaign_new'),
            {'name': 'TX Law', 'niche': 'law firm',
             'business_type': 'Law Firm', 'state': 'tx',
             'offer': self.offer.pk})
        self.assertEqual(resp.status_code, 302)
        campaign = OutreachCampaign.objects.get(slug='tx-law')
        self.assertEqual(campaign.state, 'TX')
        self.assertEqual(campaign.offer_id, self.offer.pk)

    def test_campaign_cannot_be_active_without_an_instantly_id(self):
        """There would be nowhere to push leads to. push_leads refuses
        anyway; better to refuse where the mistake is made."""
        resp = self.client.post(
            reverse('admin_dashboard:outreach_campaign_new'),
            {'name': 'Broken', 'niche': 'law firm', 'active': 'on'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            OutreachCampaign.objects.filter(slug='broken').exists())

    def test_deleting_a_campaign_keeps_its_leads(self):
        """Leads cost money to source and verify. Losing a campaign row
        must never lose them."""
        campaign = OutreachCampaign.objects.create(
            name='TX', slug='tx', niche='law firm')
        lead = Lead.objects.create(
            firm_name='Chen Law', email='a@b.com', campaign=campaign,
            source='apify')
        self.client.post(reverse(
            'admin_dashboard:outreach_campaign_delete', args=[campaign.pk]))
        lead.refresh_from_db()
        self.assertIsNone(lead.campaign_id)
        self.assertFalse(
            OutreachCampaign.objects.filter(pk=campaign.pk).exists())

    # ── review queue ───────────────────────────────────────────────────

    def test_approve_releases_the_lead(self):
        lead = Lead.objects.create(
            firm_name='Kinney Recruiting', email='a@b.com',
            source='apify', needs_review=True,
            review_reason='Name contains recruiting')
        self.client.post(
            reverse('admin_dashboard:outreach_review_decide',
                    args=[lead.pk]), {'decision': 'approve'})
        lead.refresh_from_db()
        self.assertFalse(lead.needs_review)
        self.assertNotEqual(lead.status, 'archived')
        self.assertEqual(lead.reviewed_by, self.user)

    def test_reject_archives_the_lead(self):
        lead = Lead.objects.create(
            firm_name='Bwa Video', email='a@b.com', source='apify',
            needs_review=True)
        self.client.post(
            reverse('admin_dashboard:outreach_review_decide',
                    args=[lead.pk]), {'decision': 'reject'})
        lead.refresh_from_db()
        self.assertFalse(lead.needs_review)
        self.assertEqual(lead.status, 'archived')

    def test_bulk_approve(self):
        for i in range(3):
            Lead.objects.create(firm_name=f'F{i}', email=f'{i}@b.com',
                                source='apify', needs_review=True)
        self.client.post(reverse('admin_dashboard:outreach_review_bulk'),
                         {'decision': 'approve'})
        self.assertEqual(Lead.objects.filter(needs_review=True).count(), 0)

    def test_review_decisions_are_post_only(self):
        lead = Lead.objects.create(
            firm_name='F', email='a@b.com', source='apify',
            needs_review=True)
        resp = self.client.get(reverse(
            'admin_dashboard:outreach_review_decide', args=[lead.pk]))
        self.assertEqual(resp.status_code, 405)
        lead.refresh_from_db()
        self.assertTrue(lead.needs_review)
