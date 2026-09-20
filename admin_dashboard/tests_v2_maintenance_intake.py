"""
v2 admin — maintenance intake surfacing:
  - Onboarding sequence gets a "Maintenance Intake" step, but only when
    the website has an active maintenance plan.
  - The Intake tab gets Website/Maintenance sub-tabs; the Maintenance
    one renders the onboarding.registry sections + answers.
  - Admin override to mark the maintenance Onboarding complete.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan
from onboarding.models import Onboarding, OnboardingResponse

User = get_user_model()

_seq = 0


def _staff():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'mi_staff{_seq}', email=f'mi_staff{_seq}@example.com',
        password='test-pass-123', is_staff=True, is_superuser=True)


def _account_and_website():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'mi_client{_seq}', email=f'mi_client{_seq}@example.com',
        password='x')
    account = Account.objects.create(user=u, name=f'MI Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'MI Site {_seq}', build_platform='custom')
    return account, website


class OnboardingStepVisibilityTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_step_hidden_without_active_maintenance_plan(self):
        _, website = _account_and_website()
        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=onboarding')
        r = self.client.get(url)
        self.assertNotContains(r, 'Maintenance Intake')

    def test_step_shown_with_active_maintenance_plan(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=onboarding')
        r = self.client.get(url)
        self.assertContains(r, 'Maintenance Intake')


class IntakeSubtabTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_website_subtab_is_default(self):
        _, website = _account_and_website()
        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=intake')
        r = self.client.get(url)
        self.assertContains(r, 'Submitted intake')

    def test_maintenance_subtab_no_plan(self):
        _, website = _account_and_website()
        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=intake&subtab=maintenance')
        r = self.client.get(url)
        self.assertContains(r, 'No active maintenance plan')

    def test_maintenance_subtab_renders_answers(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        ob = Onboarding.objects.create(
            user=account.user, product_type='maintenance',
            tier_slug='hvac-full-plan')
        OnboardingResponse.objects.create(
            onboarding=ob, question_key='current_site_url',
            value='https://foodtrucksofsa.com')
        OnboardingResponse.objects.create(
            onboarding=ob, question_key='current_site_age', skipped=True)

        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=intake&subtab=maintenance')
        r = self.client.get(url)
        self.assertContains(r, 'Site URL we will maintain')
        self.assertContains(r, 'https://foodtrucksofsa.com')
        self.assertContains(r, 'Skipped')


class MaintenanceOnboardingMarkCompleteTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_marks_complete_when_onboarding_exists(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        ob = Onboarding.objects.create(
            user=account.user, product_type='maintenance',
            tier_slug='hvac-full-plan')

        url = reverse(
            'admin_dashboard:v2_maintenance_onboarding_mark_complete',
            args=[website.id])
        r = self.client.post(url, follow=True)
        ob.refresh_from_db()
        self.assertIsNotNone(ob.completed_at)
        self.assertContains(r, 'Maintenance intake marked complete')

    def test_error_when_no_onboarding_exists(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        url = reverse(
            'admin_dashboard:v2_maintenance_onboarding_mark_complete',
            args=[website.id])
        r = self.client.post(url, follow=True)
        self.assertContains(r, 'No maintenance onboarding exists yet')

    def test_blocked_for_live_subscription_website(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        ob = Onboarding.objects.create(
            user=account.user, product_type='maintenance',
            tier_slug='hvac-full-plan')
        website.stripe_hosting_subscription_id = 'sub_mi_live'
        website.save(update_fields=['stripe_hosting_subscription_id'])

        url = reverse(
            'admin_dashboard:v2_maintenance_onboarding_mark_complete',
            args=[website.id])
        r = self.client.post(url, follow=True)
        ob.refresh_from_db()
        self.assertIsNone(ob.completed_at)
        self.assertContains(r, 'live Stripe subscription')
