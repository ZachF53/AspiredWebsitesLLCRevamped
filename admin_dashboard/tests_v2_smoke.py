"""
Smoke tests for the v2 dashboard (admin_dashboard/v2/) — every list page,
every website-detail tab, in a handful of different website states
(brand new / mid-build / live / wordpress), must render without a
crash. Not exhaustive of every field combination, but catches template
errors, bad {% url %} names, and missing context keys — the class of bug
most likely in a large from-scratch template set.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account, Website

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2SmokeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        u = User.objects.create_user(
            username='v2staff', email='v2staff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        cls.staff = u

        u1 = User.objects.create_user(
            username='v2client1', email='v2client1@example.com', password='x')
        cls.account_new_site = Account.objects.filter(user=u1).first() or (
            Account.objects.create(user=u1, name='Brand New Co'))
        cls.account_new_site.websites.all().delete()
        cls.website_new_site = Website.objects.create(
            account=cls.account_new_site, name='Brand New Site',
            build_platform='custom')

        u2 = User.objects.create_user(
            username='v2client2', email='v2client2@example.com', password='x')
        cls.account_live = Account.objects.filter(user=u2).first() or (
            Account.objects.create(user=u2, name='Live Co'))
        cls.account_live.websites.all().delete()
        cls.website_live = Website.objects.create(
            account=cls.account_live, name='Live Site',
            build_platform='custom', stage='live',
            payment_status='fully_paid', maintenance_active=True,
            site_status='live', status='active',
            do_droplet_id='12345', do_droplet_ip='1.2.3.4')

        u3 = User.objects.create_user(
            username='v2client3', email='v2client3@example.com', password='x')
        cls.account_wp = Account.objects.filter(user=u3).first() or (
            Account.objects.create(user=u3, name='WordPress Co'))
        cls.account_wp.websites.all().delete()
        cls.website_wp = Website.objects.create(
            account=cls.account_wp, name='WP Site',
            build_platform='wordpress', stage='pre_launch')

        u4 = User.objects.create_user(
            username='v2client4', email='v2client4@example.com', password='x')
        cls.account_archived = Account.objects.filter(user=u4).first() or (
            Account.objects.create(user=u4, name='Archived Co',
                                    status='archived'))
        cls.account_archived.websites.all().delete()
        cls.website_archived = Website.objects.create(
            account=cls.account_archived, name='Archived Site',
            status='archived', stage='live', payment_status='fully_paid')

    def setUp(self):
        self.client.force_login(self.staff)

    def test_dashboard_renders(self):
        r = self.client.get('/admin-dashboard/v2/')
        self.assertEqual(r.status_code, 200)

    def test_money_partial_renders(self):
        r = self.client.get('/admin-dashboard/v2/money-partial/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'MRR', r.content)

    def test_accounts_list_renders(self):
        r = self.client.get('/admin-dashboard/v2/accounts/')
        self.assertEqual(r.status_code, 200)

    def test_accounts_list_search_renders(self):
        r = self.client.get('/admin-dashboard/v2/accounts/?q=Live')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Live Co', r.content)

    def test_account_detail_renders(self):
        r = self.client.get(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/')
        self.assertEqual(r.status_code, 200)

    def test_account_detail_mirrors_v1_sections(self):
        r = self.client.get(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/')
        html = r.content.decode()
        for label in ('Identity', 'Mailing / WHOIS Address',
                      'Account State', 'Communication Preferences',
                      'Onboarding', 'Internal', 'Payments &amp; invoices',
                      'Login &amp; Password', 'Delete this Account'):
            with self.subTest(section=label):
                self.assertIn(label, html)
        # Delete modal markup present (JS-driven, no server round trip
        # needed to verify it rendered).
        self.assertIn('id="delete-account-modal"', html)
        self.assertIn('id="delete-account-confirm-input"', html)

    def test_account_detail_save_updates_fields(self):
        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/', {
                'name': 'Live Co Renamed',
                'contact_name': 'Jordan',
                'status': 'active',
                'preferred_contact_method': 'email',
                'onboarding_status': 'complete',
            })
        self.assertEqual(r.status_code, 302)
        self.account_live.refresh_from_db()
        self.assertEqual(self.account_live.name, 'Live Co Renamed')
        self.assertEqual(self.account_live.contact_name, 'Jordan')

    def test_account_detail_login_toggle_present_in_post_enables(self):
        """Mirrors v1's exact (checkbox-only, no hidden fallback) logic:
        the field is only written when the key is present in POST at
        all. A real unchecked HTML checkbox sends no key, so this is a
        pre-existing v1 quirk carried over deliberately, not something
        this build changed — see the account_detail view docstring."""
        self.account_live.user.is_active = False
        self.account_live.user.save(update_fields=['is_active'])

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/', {
                'name': self.account_live.name,
                'user_is_active': 'on',
            })
        self.assertEqual(r.status_code, 302)
        self.account_live.user.refresh_from_db()
        self.assertTrue(self.account_live.user.is_active)

    def test_account_create_get_renders(self):
        r = self.client.get('/admin-dashboard/v2/accounts/new/')
        self.assertEqual(r.status_code, 200)

    def test_account_create_enables_login_and_sets_pending_setup(self):
        r = self.client.post('/admin-dashboard/v2/accounts/new/', {
            'name': 'Fresh Record Co', 'email': 'freshrecord@example.com',
            'contact_name': 'Sam', 'phone': '555-9999',
        })
        self.assertEqual(r.status_code, 302)
        account = Account.objects.get(name='Fresh Record Co')
        self.assertTrue(account.user.is_active)
        self.assertEqual(account.onboarding_status, 'pending_setup')

    def test_website_create_get_renders(self):
        r = self.client.get('/admin-dashboard/v2/websites/new/')
        self.assertEqual(r.status_code, 200)

    def test_website_create_post_creates_and_redirects_to_v1(self):
        r = self.client.post('/admin-dashboard/v2/websites/new/', {
            'account_id': str(self.account_new_site.id),
            'name': 'Freshly Created Site',
            'build_platform': 'wordpress',
        })
        self.assertEqual(r.status_code, 302)
        site = Website.objects.get(name='Freshly Created Site')
        self.assertEqual(site.account_id, self.account_new_site.id)
        self.assertEqual(site.build_platform, 'wordpress')
        self.assertIn(f'/admin-dashboard/websites/{site.id}/', r.url)

    def test_website_create_requires_account_and_name(self):
        r = self.client.post('/admin-dashboard/v2/websites/new/', {
            'account_id': '', 'name': '', 'build_platform': 'custom',
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(
            Website.objects.filter(name='').exists())

    def test_account_create_post_creates_record_and_sends_nothing(self):
        from django.core import mail
        r = self.client.post('/admin-dashboard/v2/accounts/new/', {
            'name': 'New Record Co', 'email': 'newrecord@example.com',
            'contact_name': 'Pat', 'phone': '555-1234',
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            Account.objects.filter(name='New Record Co').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_websites_list_renders(self):
        r = self.client.get('/admin-dashboard/v2/websites/')
        self.assertEqual(r.status_code, 200)

    def test_websites_list_filter_renders(self):
        r = self.client.get('/admin-dashboard/v2/websites/?stage=live')
        self.assertEqual(r.status_code, 200)

    def _tabs_for(self, website, expect_infrastructure):
        tabs = ['overview', 'onboarding', 'intake', 'security', 'domains',
                'billing', 'monitoring']
        if expect_infrastructure:
            tabs.append('infrastructure')
        for tab in tabs:
            with self.subTest(website=website.name, tab=tab):
                r = self.client.get(
                    f'/admin-dashboard/v2/websites/{website.id}/?tab={tab}')
                self.assertEqual(r.status_code, 200)

    def test_every_tab_renders_for_brand_new_website(self):
        self._tabs_for(self.website_new_site, expect_infrastructure=True)

    def test_every_tab_renders_for_live_website(self):
        self._tabs_for(self.website_live, expect_infrastructure=True)

    def test_every_tab_renders_for_wordpress_website(self):
        self._tabs_for(self.website_wp, expect_infrastructure=False)

    def test_every_tab_renders_for_archived_website(self):
        self._tabs_for(self.website_archived, expect_infrastructure=True)

    def test_onboarding_tab_renders_with_an_active_reminder_cooldown(self):
        """Found via staging QA: setup_cooldown/intake_cooldown were
        computed as bare timedeltas and handed to the |timeuntil
        template filter, which expects a datetime to diff against "now"
        — this crashed with AttributeError the first time this page was
        ever viewed for an account with a recent reminder timestamp."""
        from clients.models import OnboardingToken

        OnboardingToken.objects.create(
            account_new=self.account_live,
            last_setup_reminder_at=timezone.now(),
            last_intake_reminder_at=timezone.now(),
        )
        r = self.client.get(
            f'/admin-dashboard/v2/websites/{self.website_live.id}/'
            '?tab=onboarding')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'cooldown', r.content)

    def test_infrastructure_tab_hidden_in_nav_for_wordpress(self):
        r = self.client.get(
            f'/admin-dashboard/v2/websites/{self.website_wp.id}/'
            '?tab=overview')
        self.assertNotIn(b'?tab=infrastructure', r.content)

    def test_domains_list_renders(self):
        r = self.client.get('/admin-dashboard/v2/domains/')
        self.assertEqual(r.status_code, 200)

    def test_billing_list_renders(self):
        r = self.client.get('/admin-dashboard/v2/billing/')
        self.assertEqual(r.status_code, 200)

    def test_stage_change_respects_payment_guard(self):
        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_new_site.id}/stage/',
            {'new_stage': 'live'})
        self.assertEqual(r.status_code, 302)
        self.website_new_site.refresh_from_db()
        self.assertNotEqual(self.website_new_site.stage, 'live')

    def test_stage_change_allows_valid_transition(self):
        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_new_site.id}/stage/',
            {'new_stage': 'structure'})
        self.assertEqual(r.status_code, 302)
        self.website_new_site.refresh_from_db()
        self.assertEqual(self.website_new_site.stage, 'structure')

    def test_payment_override_requires_reason(self):
        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_new_site.id}/stage/',
            {'action': 'payment_override', 'override_reason': ''})
        self.assertEqual(r.status_code, 302)
        self.website_new_site.refresh_from_db()
        self.assertNotEqual(self.website_new_site.payment_status, 'fully_paid')

    def test_payment_override_records_attestation_and_launches(self):
        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_new_site.id}/stage/',
            {'action': 'payment_override',
             'override_reason': 'Paid via Zelle, confirmed by bank statement'})
        self.assertEqual(r.status_code, 302)
        self.website_new_site.refresh_from_db()
        self.assertEqual(self.website_new_site.payment_status, 'fully_paid')
        self.assertEqual(self.website_new_site.stage, 'live')
        self.assertIsNotNone(self.website_new_site.payment_verified_at)
        self.assertIn('Zelle', self.website_new_site.payment_verification_note)

    def test_live_subscription_website_stage_write_is_blocked(self):
        """Hard constraint: no write path may touch a Website row with a
        non-null Stripe subscription id (the two real paying clients)."""
        self.website_live.stripe_hosting_subscription_id = 'sub_live123'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])
        original_stage = self.website_live.stage

        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_live.id}/stage/',
            {'new_stage': 'revisions'})
        self.assertEqual(r.status_code, 302)
        self.website_live.refresh_from_db()
        self.assertEqual(self.website_live.stage, original_stage)

    def test_live_subscription_website_payment_override_is_blocked(self):
        self.website_live.stripe_maintenance_subscription_id = 'sub_maint123'
        self.website_live.payment_status = 'deposit_paid'
        self.website_live.save(update_fields=[
            'stripe_maintenance_subscription_id', 'payment_status'])

        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_live.id}/stage/',
            {'action': 'payment_override', 'override_reason': 'test'})
        self.assertEqual(r.status_code, 302)
        self.website_live.refresh_from_db()
        self.assertEqual(self.website_live.payment_status, 'deposit_paid')

    def test_live_subscription_website_auto_send_toggle_is_blocked(self):
        self.website_live.stripe_hosting_subscription_id = 'sub_live456'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])
        before = self.website_live.auto_send_scan_reports

        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_live.id}/'
            'toggle-auto-send-scan/')
        self.assertEqual(r.status_code, 302)
        self.website_live.refresh_from_db()
        self.assertEqual(before, self.website_live.auto_send_scan_reports)

    def test_toggle_auto_send_scan(self):
        before = self.website_live.auto_send_scan_reports
        r = self.client.post(
            f'/admin-dashboard/v2/websites/{self.website_live.id}/'
            'toggle-auto-send-scan/')
        self.assertEqual(r.status_code, 302)
        self.website_live.refresh_from_db()
        self.assertNotEqual(before, self.website_live.auto_send_scan_reports)

    # ── Account delete — live-subscription guard ──

    def test_delete_button_disabled_when_website_has_live_subscription(self):
        self.website_live.stripe_hosting_subscription_id = 'sub_delete_guard1'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])

        r = self.client.get(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/')
        html = r.content.decode()
        self.assertIn('sub_delete_guard1', html)
        # The button carries `disabled` when blocked.
        btn_start = html.index('id="open-delete-account-modal"')
        btn_tag = html[btn_start:html.index('>', btn_start)]
        self.assertIn('disabled', btn_tag)

    def test_delete_button_enabled_when_no_live_subscription(self):
        r = self.client.get(
            f'/admin-dashboard/v2/accounts/{self.account_new_site.id}/')
        html = r.content.decode()
        btn_start = html.index('id="open-delete-account-modal"')
        btn_tag = html[btn_start:html.index('>', btn_start)]
        self.assertNotIn('disabled', btn_tag)

    def test_delete_refused_by_direct_post_when_live_subscription(self):
        """The guard is server-side — a crafted POST with the correct
        confirm_name must still be refused, not just the button."""
        self.website_live.stripe_maintenance_subscription_id = 'sub_delete_guard2'
        self.website_live.save(
            update_fields=['stripe_maintenance_subscription_id'])
        account_id = self.account_live.id
        user_id = self.account_live.user_id

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{account_id}/delete/',
            {'confirm_name': self.account_live.name})

        self.assertEqual(r.status_code, 302)
        self.assertTrue(Account.objects.filter(id=account_id).exists())
        self.assertTrue(Website.objects.filter(id=self.website_live.id).exists())
        self.assertTrue(User.objects.filter(id=user_id).exists())

    def test_delete_message_names_website_and_subscription(self):
        self.website_live.stripe_hosting_subscription_id = 'sub_named_123'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/delete/',
            {'confirm_name': self.account_live.name}, follow=True)

        messages = [str(m) for m in r.context['messages']]
        joined = ' '.join(messages)
        self.assertIn(self.website_live.name, joined)
        self.assertIn('sub_named_123', joined)

    def test_delete_succeeds_via_v2_when_no_live_subscription(self):
        account_id = self.account_new_site.id
        website_id = self.website_new_site.id
        user_id = self.account_new_site.user_id
        account_name = self.account_new_site.name

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{account_id}/delete/',
            {'confirm_name': account_name})

        self.assertEqual(r.status_code, 302)
        self.assertFalse(Account.objects.filter(id=account_id).exists())
        self.assertFalse(Website.objects.filter(id=website_id).exists())
        self.assertFalse(User.objects.filter(id=user_id).exists())

    def test_v1_account_delete_view_unchanged_still_has_no_guard(self):
        """Confirms this fix did not touch v1's view — v1 keeps deleting
        a live-subscription account when posted to directly, exactly as
        before. This is a characterization test of v1's existing
        behavior, not an endorsement of it."""
        self.website_live.stripe_hosting_subscription_id = 'sub_v1_unchanged'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])
        account_id = self.account_live.id

        r = self.client.post(
            f'/admin-dashboard/accounts/{account_id}/delete/',
            {'confirm_name': self.account_live.name})

        self.assertEqual(r.status_code, 302)
        self.assertFalse(Account.objects.filter(id=account_id).exists())

    # ── Account field editor — live-subscription guard ──

    def test_account_edit_refused_when_website_has_live_subscription(self):
        self.website_live.stripe_hosting_subscription_id = 'sub_edit_guard1'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])
        original_name = self.account_live.name

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/', {
                'name': 'Renamed While Blocked',
                'contact_name': 'Should Not Save',
                'status': 'active',
            })

        self.assertEqual(r.status_code, 302)
        self.account_live.refresh_from_db()
        self.assertEqual(self.account_live.name, original_name)

    def test_account_edit_login_toggle_refused_when_live_subscription(self):
        """The user_is_active toggle writes via the same POST handler —
        confirm the guard blocks it too, not just the Account fields."""
        self.website_live.stripe_maintenance_subscription_id = 'sub_edit_guard2'
        self.website_live.save(
            update_fields=['stripe_maintenance_subscription_id'])
        self.account_live.user.is_active = False
        self.account_live.user.save(update_fields=['is_active'])

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/', {
                'name': self.account_live.name,
                'user_is_active': 'on',
            })

        self.assertEqual(r.status_code, 302)
        self.account_live.user.refresh_from_db()
        self.assertFalse(self.account_live.user.is_active)

    def test_account_edit_message_names_website_and_subscription(self):
        self.website_live.stripe_hosting_subscription_id = 'sub_edit_named_1'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])

        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/',
            {'name': 'Attempted Rename'}, follow=True)

        messages = [str(m) for m in r.context['messages']]
        joined = ' '.join(messages)
        self.assertIn(self.website_live.name, joined)
        self.assertIn('sub_edit_named_1', joined)

    def test_account_edit_controls_disabled_when_live_subscription(self):
        self.website_live.stripe_hosting_subscription_id = 'sub_edit_disabled1'
        self.website_live.save(update_fields=['stripe_hosting_subscription_id'])

        r = self.client.get(
            f'/admin-dashboard/v2/accounts/{self.account_live.id}/')
        html = r.content.decode()
        self.assertIn('sub_edit_disabled1', html)
        save_start = html.index('Save Account')
        save_tag_start = html.rindex('<button', 0, save_start)
        save_tag = html[save_tag_start:html.index('>', save_tag_start)]
        self.assertIn('disabled', save_tag)

    def test_account_edit_controls_enabled_when_no_live_subscription(self):
        r = self.client.get(
            f'/admin-dashboard/v2/accounts/{self.account_new_site.id}/')
        html = r.content.decode()
        save_start = html.index('Save Account')
        save_tag_start = html.rindex('<button', 0, save_start)
        save_tag = html[save_tag_start:html.index('>', save_tag_start)]
        self.assertNotIn('disabled', save_tag)

    def test_account_edit_still_works_when_no_live_subscription(self):
        r = self.client.post(
            f'/admin-dashboard/v2/accounts/{self.account_new_site.id}/', {
                'name': 'Brand New Co Renamed',
                'contact_name': 'Someone',
            })
        self.assertEqual(r.status_code, 302)
        self.account_new_site.refresh_from_db()
        self.assertEqual(self.account_new_site.name, 'Brand New Co Renamed')
