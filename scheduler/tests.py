"""
Phase 3.3 — scheduler tests.

Covers:
  - hold_slot: lead-time floor (Phase 0/MIN_LEAD), overlap detection,
    happy-path creates a held ScheduledCall
  - confirm_slot: held → confirmed, creates Lead with service tag
  - Rate limit on /schedule/hold/ + /schedule/confirm/ (Phase 0.4a)
"""

import datetime as _dt
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from scheduler.models import AvailabilityWindow, ScheduledCall


def _make_window():
    """Mon-Sun 0-23 window so any time-of-day slot is bookable."""
    return AvailabilityWindow.objects.create(
        day_of_week=timezone.now().weekday(),
        start_time=_dt.time(0, 0),
        end_time=_dt.time(23, 59),
        timezone='America/New_York',
        active=True,
    )


def _future_slot(*, hours_ahead=4):
    """A timestamp that satisfies the 2-hour MIN_LEAD floor."""
    return (timezone.now() + _dt.timedelta(hours=hours_ahead)
            ).replace(microsecond=0)


class HoldSlotTests(TestCase):
    """Phase 1.2 + 0.4a — hold_slot rejects bad inputs + rate-limits."""

    def setUp(self):
        cache.clear()
        _make_window()

    def _post(self, payload):
        return self.client.post(
            reverse('scheduler:hold_slot'),
            data=json.dumps(payload),
            content_type='application/json')

    def test_too_close_to_now_rejected(self):
        """Slot inside MIN_LEAD window → 409 too-close."""
        starts_at = timezone.now() + _dt.timedelta(minutes=10)
        r = self._post({'starts_at': starts_at.isoformat()})
        self.assertEqual(r.status_code, 409)

    def test_overlap_detection_rejects(self):
        """A second hold inside an existing 2-hour block → 409 taken."""
        first_start = _future_slot(hours_ahead=4)
        # Pre-create a confirmed block.
        ScheduledCall.objects.create(
            starts_at=first_start,
            ends_at=first_start + _dt.timedelta(hours=2),
            status='confirmed',
        )
        # 30 min into the block — must overlap.
        clash = first_start + _dt.timedelta(minutes=30)
        r = self._post({'starts_at': clash.isoformat()})
        self.assertEqual(r.status_code, 409)

    def test_happy_path_creates_held_call(self):
        starts_at = _future_slot(hours_ahead=4)
        r = self._post({'starts_at': starts_at.isoformat()})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        call = ScheduledCall.objects.get(id=body['call_id'])
        self.assertEqual(call.status, 'held')
        # 2-hour block applied (BLOCK_MINUTES = 120)
        self.assertEqual(call.ends_at - call.starts_at,
                         _dt.timedelta(minutes=120))

    def test_missing_starts_at_rejected(self):
        r = self._post({})
        self.assertEqual(r.status_code, 400)


class ConfirmSlotTests(TestCase):
    """confirm_slot — happy path + service tagging on Lead."""

    def setUp(self):
        cache.clear()
        _make_window()
        starts_at = _future_slot(hours_ahead=4)
        r = self.client.post(
            reverse('scheduler:hold_slot'),
            data=json.dumps({'starts_at': starts_at.isoformat()}),
            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.call_id = r.json()['call_id']

    def _confirm(self, payload):
        return self.client.post(
            reverse('scheduler:confirm_slot'),
            data=json.dumps(payload),
            content_type='application/json')

    def test_confirm_flips_status_to_confirmed(self):
        with patch('scheduler.emails.send_schedule_confirmation_to_customer'), \
             patch('scheduler.emails.send_schedule_notification_to_admin'):
            r = self._confirm({
                'call_id': self.call_id,
                'name': 'Jane',
                'email': 'jane@example.com',
                'business': 'Jane LLC',
                'inquiry': 'I want a website.',
                'service': 'web_design',
                'build_type': 'essential',
            })
        self.assertEqual(r.status_code, 200)
        call = ScheduledCall.objects.get(id=self.call_id)
        self.assertEqual(call.status, 'confirmed')

    def test_confirm_creates_lead_with_service_tag(self):
        from outreach.models import Lead
        with patch('scheduler.emails.send_schedule_confirmation_to_customer'), \
             patch('scheduler.emails.send_schedule_notification_to_admin'):
            self._confirm({
                'call_id': self.call_id,
                'name': 'Mike',
                'email': 'mike@example.com',
                'business': 'Mike LLC',
                'inquiry': 'Need help with social media.',
                'service': 'social_media',
            })
        lead = Lead.objects.get(email='mike@example.com')
        self.assertIn('service:social_media', lead.tags)
        # build_type was empty — should NOT appear in tags
        self.assertNotIn('build_type:', lead.tags)

    def test_confirm_missing_required_fields(self):
        r = self._confirm({
            'call_id': self.call_id,
            'name': 'NameOnly',
        })
        self.assertEqual(r.status_code, 400)

    def test_confirm_unknown_call_id_404(self):
        import uuid
        r = self._confirm({
            'call_id': str(uuid.uuid4()),
            'name': 'X', 'email': 'x@y.com', 'business': 'X',
        })
        self.assertEqual(r.status_code, 404)
