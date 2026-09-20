"""
- Portal nav: Domains and SEO & Conversions are hidden (parked for
  reimplementation); Invoices moved from Support to the Account group.
- My Project page: Site Uptime section hidden for WordPress builds
  (client-hosted, no droplet for us to monitor).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website

User = get_user_model()


class PortalNavTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='navtest', email='navtest@example.com',
            password='pw-123456')
        self.account = Account.objects.create(
            user=self.user, name='Nav Test Co', onboarding_status='complete')
        Website.objects.create(
            account=self.account, name='Nav Test Site',
            onboarding_status='complete')
        self.client.force_login(self.user)

    def test_domains_link_hidden(self):
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertNotContains(resp, '>Domains<')

    def test_seo_conversions_link_hidden(self):
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertNotContains(resp, 'SEO &amp; Conversions')

    def test_invoices_link_still_present(self):
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertContains(resp, '>Invoices<')


class ProjectPageUptimeTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='projwp', email='projwp@example.com',
            password='pw-123456')
        self.account = Account.objects.create(
            user=self.user, name='Project WP Co', onboarding_status='complete')
        self.client.force_login(self.user)

    def test_custom_build_shows_site_uptime(self):
        Website.objects.create(
            account=self.account, name='Custom Proj Site',
            onboarding_status='complete', build_platform='custom')
        resp = self.client.get(reverse('clients:project'))
        self.assertContains(resp, 'Site Uptime')

    def test_wordpress_build_hides_site_uptime(self):
        Website.objects.create(
            account=self.account, name='WP Proj Site',
            onboarding_status='complete', build_platform='wordpress')
        resp = self.client.get(reverse('clients:project'))
        self.assertNotContains(resp, 'Site Uptime')
