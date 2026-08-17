"""
The tracker snippet's id must keep resolving after the cutover.

`<script data-aspired-client="UUID">` sits in the HTML of client sites we
do not control and cannot redeploy. Every snippet already out there
carries a ClientProfile id. If the ingest endpoints only accepted Website
ids, every one of those sites would silently stop reporting conversions —
no error anywhere, just a client watching their numbers go to zero.

So both forms resolve, and both are pinned here.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.models import ClientProfile
from reporting.models import ConversionEvent

User = get_user_model()


class TrackerIdResolutionTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(
            username='tracker-id', password='x',
            email='tracker@example.com')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Tracker Co')
        self.account = self.profile.migrated_account
        self.site = self.account.websites.first()
        self.url = reverse('reporting:track')

    def _post(self, client_id):
        return self.client.post(
            self.url,
            data=json.dumps({
                'client_id': str(client_id),
                'event_type': 'form_submit',
            }),
            content_type='application/json')

    def test_a_website_id_records_against_that_site(self):
        self.assertEqual(self._post(self.site.id).status_code, 200)
        event = ConversionEvent.objects.get()
        self.assertEqual(event.website_new_id, self.site.id)

    def test_a_legacy_profile_id_still_records(self):
        """The id in every snippet deployed before the cutover."""
        self.assertEqual(self._post(self.profile.id).status_code, 200)
        event = ConversionEvent.objects.get()
        self.assertEqual(event.website_new_id, self.site.id)

    def test_an_unknown_id_is_accepted_and_dropped(self):
        """The endpoint is public and CORS-open, so it must never leak
        whether an id exists — a 200 with nothing written is correct."""
        import uuid

        self.assertEqual(self._post(uuid.uuid4()).status_code, 200)
        self.assertEqual(ConversionEvent.objects.count(), 0)

    def test_a_malformed_id_does_not_error(self):
        self.assertEqual(self._post('not-a-uuid').status_code, 200)
        self.assertEqual(ConversionEvent.objects.count(), 0)


class SessionRecordingIdResolutionTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(
            username='rec-id', password='x', email='rec@example.com')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Rec Co')
        self.site = self.profile.migrated_account.websites.first()
        self.site.session_recording_enabled = True
        self.site.save(update_fields=['session_recording_enabled'])

    def _config(self, client_id):
        url = reverse('reporting:tracker_config', args=[str(client_id)])
        return json.loads(self.client.get(url).content)

    def test_a_legacy_id_still_reports_the_sites_tier(self):
        """`session_recording_enabled` moved to Website, and the snippet
        asking the question carries a legacy profile id."""
        payload = self._config(self.profile.id)
        self.assertTrue(payload['session_recording'])
        self.assertEqual(payload['tier'], 2)

    def test_a_website_id_reports_the_same(self):
        payload = self._config(self.site.id)
        self.assertTrue(payload['session_recording'])

    def test_disabled_on_the_site_reports_tier_one(self):
        self.site.session_recording_enabled = False
        self.site.save(update_fields=['session_recording_enabled'])
        payload = self._config(self.profile.id)
        self.assertFalse(payload['session_recording'])
        self.assertEqual(payload['tier'], 1)

    def test_an_unknown_id_gets_the_safe_default(self):
        """A typoed UUID must not reveal whether the id exists."""
        import uuid

        payload = self._config(uuid.uuid4())
        self.assertEqual(payload['tier'], 1)
        self.assertFalse(payload['session_recording'])
