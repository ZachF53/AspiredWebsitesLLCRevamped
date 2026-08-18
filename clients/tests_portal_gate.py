"""
The portal gate must never loop, and must never lock an owner out.

883 tests passed while the live portal was in an infinite redirect loop.
Rendering the real pages on staging is what found it: the onboarding gate
moved onto `Account.onboarding_status`, staging carried a stale
`pending_setup` (stamped by the autocreate signal on a client who had
been set up long before), there was no usable token, and the fallback
bounced an ALREADY-AUTHENTICATED user to the login page — which sees a
valid session and sends them straight back.

No unit test covered it because each redirect is individually correct.
Only following the chain shows the cycle.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website

User = get_user_model()


class OnboardingGateTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='gate', email='gate@example.com', password='pw-123456')
        self.account = Account.objects.create(
            user=self.user, name='Gate Co', onboarding_status='complete')
        self.site = Website.objects.create(
            account=self.account, name='Gate Site',
            onboarding_status='complete')
        self.client.force_login(self.user)

    def test_a_settled_client_reaches_the_portal(self):
        resp = self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertEqual(resp.status_code, 200)

    def test_a_stale_pending_setup_admits_rather_than_looping(self):
        """The exact staging failure. `pending_setup` with no usable
        token used to redirect to login, which redirects back."""
        self.account.onboarding_status = 'pending_setup'
        self.account.save(update_fields=['onboarding_status'])

        resp = self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertEqual(resp.status_code, 200)

    def test_the_stale_flag_raises_an_alert_so_it_gets_fixed(self):
        """Admitting them is the safe call, not the correct end state."""
        from unittest.mock import patch

        self.account.onboarding_status = 'pending_setup'
        self.account.save(update_fields=['onboarding_status'])

        with patch('core.system_alerts.record_alert') as alert:
            self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertEqual(alert.call_count, 1)
        self.assertEqual(alert.call_args.kwargs['severity'], 'error')

    def test_a_site_still_owing_an_intake_is_sent_to_the_form(self):
        self.site.onboarding_status = 'pending_intake'
        self.site.save(update_fields=['onboarding_status'])

        resp = self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            reverse('clients:intake'),
            [url for url, _code in resp.redirect_chain])

    def test_the_intake_form_itself_is_reachable_while_pending(self):
        """Otherwise the pending_intake redirect is its own loop."""
        self.site.onboarding_status = 'pending_intake'
        self.site.save(update_fields=['onboarding_status'])

        resp = self.client.get(reverse('clients:intake'), follow=True)
        self.assertEqual(resp.status_code, 200)

    def test_a_logged_in_user_with_no_account_does_not_loop(self):
        """Same trap as the stale flag, different branch. Sending an
        authenticated non-client to the login page loops: login sees a
        valid session and bounces them back."""
        stranger = User.objects.create_user(
            username='stranger', password='pw-123456')
        self.client.force_login(stranger)

        resp = self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertLess(
            len(resp.redirect_chain), 5,
            f'redirect chain looks like a loop: {resp.redirect_chain}')


class PortalPagesRenderTests(TestCase):
    """Every portal page, followed to a terminal response.

    `follow=True` is the point: it is what turns a redirect cycle into a
    RedirectCycleError instead of a passing 302 assertion.
    """

    PAGES = [
        'clients:dashboard', 'clients:project', 'clients:revisions',
        'clients:support', 'clients:portal_changelog',
        'clients:portal_reports', 'clients:invoices',
        'clients:portal_maintenance', 'clients:settings',
    ]

    def setUp(self):
        user = User.objects.create_user(
            username='pages', email='pages@example.com', password='pw-123456')
        account = Account.objects.create(
            user=user, name='Pages Co', onboarding_status='complete')
        Website.objects.create(
            account=account, name='Pages Site',
            onboarding_status='complete')
        self.client.force_login(user)

    def test_every_portal_page_reaches_a_terminal_response(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                resp = self.client.get(reverse(name), follow=True)
                self.assertEqual(resp.status_code, 200)
