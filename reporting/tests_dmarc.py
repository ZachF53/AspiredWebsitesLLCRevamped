"""
Regression tests for the DMARC ingest + dashboard.

Covers the failure that kept the dashboard blank for two months: the
poller pointed at an empty Gmail label, and every layer downstream
reported success. These lock down the three things that made it
invisible — mailbox names with spaces failing SELECT, encoded subjects
never matching, and the dashboard rendering "nothing ingested" when it
really meant "nothing in the last 30 days".
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from reporting.models import DmarcReport


def _mk(report_id, days_ago, msgs=10):
    now = timezone.now()
    return DmarcReport.objects.create(
        org_name='google.com', org_email='x@google.com', report_id=report_id,
        period_start=now - timedelta(days=days_ago + 1),
        period_end=now - timedelta(days=days_ago),
        policy_domain='aspiredwebsites.com', policy_p='none', policy_pct=100,
        total_messages=msgs, dmarc_pass=msgs, dmarc_fail=0,
        dkim_pass=msgs, dkim_fail=0, spf_pass=msgs, spf_fail=0, raw_xml='<x/>')


class DmarcDashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.u = User.objects.create_superuser(
            username='admin_tmp', email='a@b.com', password='pw12345!x')
        self.c = Client()
        self.c.force_login(self.u)

    def _get(self, qs=''):
        return self.c.get('/admin-dashboard/dmarc/' + qs)

    def test_empty(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context['has_any_reports'])
        self.assertContains(r, 'No reports ingested yet')

    def test_in_window(self):
        _mk('r-recent', 3)
        r = self._get()
        self.assertEqual(r.context['total_reports'], 1)
        self.assertEqual(r.context['window_days'], 30)
        self.assertEqual(len(r.context['trend']), 30)
        self.assertTrue(r.context['has_any_reports'])

    def test_outside_window_still_listed_and_flagged(self):
        _mk('r-old', 200)
        r = self._get()
        self.assertEqual(r.context['total_reports'], 0)
        # The regression this fixes: report exists, window hides it, but
        # the recent table must still show it and the copy must say so.
        self.assertTrue(r.context['has_any_reports'])
        self.assertEqual(len(r.context['recent_reports']), 1)
        self.assertContains(r, 'older reports exist')
        self.assertNotContains(r, 'No reports ingested yet')

    def test_widened_window_includes_old(self):
        _mk('r-old2', 200)
        r = self._get('?days=365')
        self.assertEqual(r.context['total_reports'], 1)
        self.assertEqual(r.context['window_days'], 365)
        # Was `== 90` ("bar cap"). That cap WAS the bug: 90 daily
        # columns at an 18px minimum is ~2000px, wider than the card,
        # so the 1-year view pushed the page sideways. Long windows are
        # now grouped into months instead of truncated into days.
        self.assertEqual(r.context['trend_group'], 'month')
        self.assertLessEqual(
            len(r.context['trend']), 52,
            'too many columns — the chart will overflow its card')

    def test_bad_and_extreme_days_params(self):
        for qs, expect in [('?days=abc', 30), ('?days=-5', 1),
                           ('?days=99999', 365), ('?days=', 30)]:
            r = self._get(qs)
            self.assertEqual(r.status_code, 200, qs)
            self.assertEqual(r.context['window_days'], expect, qs)


class HelperTests(TestCase):
    def test_imap_quote_and_header_decode(self):
        import importlib
        m = importlib.import_module(
            'reporting.management.commands.ingest_dmarc_imap')
        self.assertEqual(m._imap_quote('[Gmail]/All Mail'),
                         '"[Gmail]/All Mail"')
        self.assertEqual(m._imap_quote('INBOX'), '"INBOX"')
        # Real Microsoft subject from the mailbox.
        enc = ('=?utf-8?B?UmVwb3J0IERvbWFpbjogYXNwaXJlZHdlYnNpdGVzLmNvbSBTdWJt'
               'aXR0ZXI6IHByb3RlY3Rpb24ub3V0bG9vay5jb20=?=')
        dec = m._decode_mime_header(enc)
        self.assertIn('Report Domain', dec)
        # Previously only the From matched; now the subject does too.
        self.assertTrue(m._looks_like_dmarc(dec, 'someone@example.com'))
        self.assertFalse(m._looks_like_dmarc(enc, 'someone@example.com'))
        self.assertEqual(m._decode_mime_header(''), '')
