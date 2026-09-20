"""Dashboard: once a site is live, "Revisions Used" and the "Project
Progress" stepper are hidden — nothing left to track once launched."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website

User = get_user_model()


class DashboardLiveHidesProgressTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='livehide', email='livehide@example.com',
            password='pw-123456')
        self.account = Account.objects.create(
            user=self.user, name='Live Hide Co', onboarding_status='complete')
        self.site = Website.objects.create(
            account=self.account, name='Live Hide Site',
            onboarding_status='complete', stage='design')
        self.client.force_login(self.user)

    def test_pre_launch_shows_revisions_and_progress(self):
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertContains(resp, 'Revisions Used')
        self.assertContains(resp, 'Project Progress')

    def test_live_hides_revisions_and_progress(self):
        self.site.stage = 'live'
        self.site.save(update_fields=['stage'])
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertNotContains(resp, 'Revisions Used')
        self.assertNotContains(resp, 'Project Progress')
