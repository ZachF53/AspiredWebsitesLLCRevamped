"""
Admin "mark intake complete" override — v1 button (thin caller, was a
duplicated implementation) and v2 button (new in this build). Both
call the same clients.services.mark_intake_complete.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website
from clients.models import IntakeResponse

User = get_user_model()

_seq = 0


def _staff():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'imc_staff{_seq}', email=f'imc_staff{_seq}@example.com',
        password='test-pass-123', is_staff=True, is_superuser=True)


def _account_and_website(**website_kwargs):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'imc_client{_seq}', email=f'imc_client{_seq}@example.com',
        password='x')
    account = Account.objects.create(user=u, name=f'IMC Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'IMC Site {_seq}', build_platform='custom',
        **website_kwargs)
    return account, website


class V1IntakeMarkCompleteTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_post_flips_onboarding_status_and_redirects(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        url = reverse('admin_dashboard:website_intake_mark_complete',
                       args=[website.id])
        r = self.client.post(url, follow=True)
        self.assertEqual(r.status_code, 200)
        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'intake_complete')
        self.assertContains(r, 'Intake marked complete')

    def test_get_is_refused(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        url = reverse('admin_dashboard:website_intake_mark_complete',
                       args=[website.id])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 405)


class V2IntakeMarkCompleteTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_post_flips_onboarding_status_and_completes_intake(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        url = reverse('admin_dashboard:v2_website_intake_mark_complete',
                       args=[website.id])
        r = self.client.post(url, follow=True)
        self.assertEqual(r.status_code, 200)

        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'intake_complete')
        self.assertTrue(IntakeResponse.objects.filter(
            website_new=website, completed=True).exists())
        self.assertContains(r, 'Intake marked complete')

    def test_blocked_for_live_subscription_website(self):
        _, website = _account_and_website(
            onboarding_status='pending_intake',
            stripe_hosting_subscription_id='sub_imc_live')
        url = reverse('admin_dashboard:v2_website_intake_mark_complete',
                       args=[website.id])
        r = self.client.post(url, follow=True)
        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'pending_intake')
        self.assertContains(r, 'live Stripe subscription')

    def test_get_redirects_without_writing(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        url = reverse('admin_dashboard:v2_website_intake_mark_complete',
                       args=[website.id])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 302)
        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'pending_intake')

    def test_onboarding_tab_shows_mark_complete_button_when_incomplete(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=onboarding')
        r = self.client.get(url)
        self.assertContains(r, 'Mark complete (no droplet)')
