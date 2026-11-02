"""
Every custom-field key the website sends must have a pinned GHL id.

Why this exists
---------------
Custom fields were addressed by KEY, on the assumption that a key always
resolves to the matching fieldKey in GHL. It mostly did -- but the
contact form sent ``gclid`` and GHL's field is ``ad_click_id`` ("Google
Ads Click ID"). So every Google Ads click id the form collected was
dropped, silently, from the first submission onwards. Nothing errored:
GHL accepted the upsert and ignored the entry.

That is the failure mode worth a test. A dropped custom field has no
symptom at the call site, so it can only be caught by comparing the keys
the code sends against the ids we hold.

This test is deliberately a STATIC check against settings. It does not
call GHL -- tests must never touch a live CRM (settings_test blanks the
credentials for exactly that reason). Verifying the ids still exist in
GHL is a job for the refresh one-liner in docs/ghl_field_ids.md, run by
hand after any change in the GHL UI.
"""
from django.test import TestCase, override_settings

from outreach import ghl


# Every custom-field key written by a website -> GHL path, with the
# call site, so a failure says where to look.
#
# Keep in sync by hand. A key added to a payload without being added
# here is not caught by this test -- but a key added here without an id
# in settings is, which is the direction that actually breaks.
SENT_KEYS = {
    # public.views._push_lead_to_ghl (contact form)
    'utm_source': 'contact form',
    'utm_medium': 'contact form',
    'utm_campaign': 'contact form',
    'utm_term': 'contact form',
    'utm_content': 'contact form',
    'ad_click_id': 'contact form (Google Ads click id; NOT "gclid")',
    'fbclid': 'contact form',
    'landing_page': 'contact + callback + audit',
    'referrer': 'contact + callback + audit',
    'trade': 'contact form',
    'trucks': 'contact form',
    'software': 'contact form',
    'project_type': 'contact form',
    'budget_range': 'contact form',
    'timeline': 'contact form',
    'current_website': 'contact form',
    'lead_message': 'contact form',
    'sms_consent': 'contact form',
    # public.views._push_callback_to_ghl
    'best_time': 'callback form',
    'source_page': 'callback form',
    # outreach.ghl.build_payload, when a phone cannot be parsed to E.164
    'phone_raw': 'build_payload unparseable-phone fallback',
    # public.views._audit_lead_to_crm
    'audit_url': 'audit capture',
    'audit_performance_score': 'audit capture',
    'audit_seo_score': 'audit capture',
    'audit_best_practices_score': 'audit capture',
    'audit_accessibility_score': 'audit capture',
    'audit_summary': 'audit capture',
    'audit_status': 'audit capture',
    'audit_checked_at': 'audit capture',
}

# Canonical audit set (report Part 2). All ten must be pinned even
# though the form path only sends eight -- audit_final_url and
# audit_lcp_ms are written by the voice path in Phase 3, and the ids
# have to be in place before that can be built against them.
CANONICAL_AUDIT_KEYS = [
    'audit_url', 'audit_final_url', 'audit_performance_score',
    'audit_seo_score', 'audit_accessibility_score',
    'audit_best_practices_score', 'audit_lcp_ms', 'audit_summary',
    'audit_status', 'audit_checked_at',
]


class GHLFieldIdCoverageTests(TestCase):

    def test_every_sent_key_has_a_pinned_id(self):
        from django.conf import settings
        ids = settings.GHL_CUSTOM_FIELD_IDS
        missing = [f'{k!r} (sent by {where})'
                   for k, where in sorted(SENT_KEYS.items()) if k not in ids]
        self.assertEqual(
            missing, [],
            'these keys would be sent by key and silently dropped by GHL; '
            'create the field in GHL and pin its id in '
            'settings.GHL_CUSTOM_FIELD_IDS:\n  ' + '\n  '.join(missing))

    def test_gclid_is_not_sent_as_a_field_key(self):
        """The specific bug. GHL has no 'gclid' field."""
        from django.conf import settings
        self.assertNotIn('gclid', settings.GHL_CUSTOM_FIELD_IDS)
        self.assertNotIn('gclid', SENT_KEYS)

    def test_all_ten_canonical_audit_fields_are_pinned(self):
        from django.conf import settings
        ids = settings.GHL_CUSTOM_FIELD_IDS
        missing = [k for k in CANONICAL_AUDIT_KEYS if k not in ids]
        self.assertEqual(missing, [], f'unpinned audit fields: {missing}')

    def test_ids_are_unique(self):
        """Two keys pointing at one id means one of them writes to the
        wrong field -- a copy-paste slip the dict shape cannot prevent."""
        from django.conf import settings
        ids = settings.GHL_CUSTOM_FIELD_IDS
        seen = {}
        dupes = []
        for key, fid in ids.items():
            if fid in seen:
                dupes.append(f'{fid} used by {seen[fid]!r} and {key!r}')
            seen[fid] = key
        self.assertEqual(dupes, [], '\n'.join(dupes))

    def test_entry_uses_id_when_pinned(self):
        entry = ghl._custom_field_entry('audit_performance_score', '41')
        self.assertEqual(entry, {'id': 'jlSbiLhjOPx8JLIysi1B',
                                 'fieldValue': '41'})

    @override_settings(GHL_CUSTOM_FIELD_IDS={})
    def test_entry_falls_back_to_key_and_warns(self):
        """The fallback is kept -- dropping a field is worse than sending
        one GHL might ignore -- but it must be loud."""
        with self.assertLogs('outreach.ghl', level='WARNING') as logs:
            entry = ghl._custom_field_entry('something_new', 'v')
        self.assertEqual(entry, {'key': 'something_new', 'fieldValue': 'v'})
        self.assertIn('something_new', logs.output[0])


class GHLLocationGuardTests(TestCase):
    """GHL_LOCATION_ID no longer defaults to the production location.

    Staging holds a valid API key, so a default meant staging wrote
    contacts into the live CRM. Unset must mean "do not push".
    """

    @override_settings(GHL_LOCATION_ID='', GHL_API_KEY='k')
    def test_unset_location_refuses_to_build_a_payload(self):
        with self.assertRaises(ghl.GHLNotConfigured):
            ghl.build_payload(first_name='A', email='a@b.com')

    @override_settings(GHL_LOCATION_ID='', GHL_API_KEY='k')
    def test_not_configured_is_a_ghl_error_so_callers_already_catch_it(self):
        """Callers catch GHLError and fall back to the email + a
        FailedLeadSubmission row. If this stopped being a subclass, an
        unset location would 500 the form instead of saving the lead."""
        self.assertTrue(issubclass(ghl.GHLNotConfigured, ghl.GHLError))
        with self.assertRaises(ghl.GHLError):
            ghl.build_payload(first_name='A', email='a@b.com')

    @override_settings(GHL_LOCATION_ID='loc_abc', GHL_API_KEY='k')
    def test_set_location_is_used(self):
        payload = ghl.build_payload(first_name='A', email='a@b.com')
        self.assertEqual(payload['locationId'], 'loc_abc')
