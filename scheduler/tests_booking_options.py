"""The /design/schedule/ "Build type" select: exactly six options,
prices from ServiceTier, and the confirm handler accepts only those."""

import datetime as dt
import json
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from scheduler.models import ScheduledCall
from scheduler.views import BUILD_TYPE_OPTIONS, build_type_choices

EXPECTED_VALUES = ['build_full', 'build_installment', 'full_plan',
                   'hosting_only', 'multi_location', 'not_sure']


class BuildTypeOptionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing', stdout=MagicMock())

    def test_exactly_six_options_in_order(self):
        self.assertEqual([v for v, _, _ in BUILD_TYPE_OPTIONS],
                         EXPECTED_VALUES)

    def test_labels_carry_servicetier_prices(self):
        labels = dict(build_type_choices())
        self.assertEqual(labels['build_full'],
                         'Website Build — Pay in Full ($2,000)')
        self.assertEqual(labels['build_installment'],
                         'Website Build — 24-Month Installment ($105/mo)')
        self.assertEqual(
            labels['hosting_only'],
            'Hosting + Security Only ($45/mo) — I already have a site')
        self.assertEqual(labels['not_sure'], 'Not sure yet')

    def test_price_follows_the_tier(self):
        from billing.pricing_models import ServiceTier
        ServiceTier.objects.filter(slug='hvac-build-full').update(
            price=2200, price_display='')
        self.assertIn('($2,200)', dict(build_type_choices())['build_full'])

    def test_page_renders_six_options_and_no_addon_fieldset(self):
        r = self.client.get('/design/schedule/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for value in EXPECTED_VALUES:
            self.assertIn(f'value="{value}"', html)
        self.assertNotIn('value="essential"', html)
        self.assertNotIn('value="premium"', html)
        self.assertNotIn('Save 10% on your first month', html)
        self.assertNotIn('name="addons"', html)


class ConfirmBuildTypeTests(TestCase):

    def setUp(self):
        from scheduler.tests import _make_window
        cache.clear()
        _make_window()

    def _hold(self):
        ScheduledCall.objects.all().delete()
        start = (timezone.now() + dt.timedelta(hours=4)).replace(
            microsecond=0)
        r = self.client.post(
            reverse('scheduler:hold_slot'),
            data=json.dumps({'starts_at': start.isoformat()}),
            content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()['call_id']

    def _confirm(self, build_type, email):
        call_id = self._hold()
        with patch('scheduler.emails.send_schedule_confirmation_to_customer'), \
             patch('scheduler.emails.send_schedule_notification_to_admin'):
            return self.client.post(
                reverse('scheduler:confirm_slot'),
                data=json.dumps({
                    'call_id': call_id, 'name': 'Hank', 'email': email,
                    'business': 'Hank HVAC', 'inquiry': 'A site',
                    'service': 'web_design', 'build_type': build_type}),
                content_type='application/json')

    def test_accepts_each_option_and_maps_package(self):
        from clients.account_models import Website
        expected_pkg = {'build_full': 'hvac_build',
                        'build_installment': 'hvac_build',
                        'full_plan': 'hvac_full_plan',
                        'hosting_only': 'hvac_hosting_security',
                        'multi_location': '', 'not_sure': ''}
        for i, value in enumerate(EXPECTED_VALUES):
            email = f'hank{i}@example.com'
            r = self._confirm(value, email)
            self.assertEqual(r.status_code, 200, (value, r.content))
            web = Website.objects.get(account__user__email=email)
            self.assertEqual(web.package, expected_pkg[value], value)

    def test_rejects_old_and_unknown_values(self):
        for i, value in enumerate(['essential', 'premium', 'website-premium',
                                   'full-plan']):
            r = self._confirm(value, f'bad{i}@example.com')
            self.assertEqual(r.status_code, 400, value)
