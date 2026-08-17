"""
Nothing may exist only on the legacy models.

The cutover contract is that dropping ClientProfile and Project loses no
information. That is easy to check for rows -- the parity audit does it --
and easy to miss for *columns*. Six load-bearing fields turned out to have
no canonical home at all:

    gbp_location_name            the Google listing binding
    do_snapshot_id               the 60-day retention snapshot's id
    site_status                  live / maintenance / offline / destroyed
    payment_failure_started_at   the escalation guard
    payment_failure_offenses     the 1st-free / 2nd-costs-$75 counter
    stripe_social_subscription_id

Every one was written by live code and read by a scheduled task. The
staged drop migration would have taken all six. The parity audit could
never have caught it, because it compares fields that exist on both
sides -- a field on only one side is invisible to it by construction.

So this test asserts the property directly, and will fail the moment
someone adds a seventh.
"""

from django.test import SimpleTestCase


# Legacy field -> why it needs no canonical column. Anything not listed
# here must exist on Account or Website.
DELIBERATELY_LEGACY_ONLY = {
    # Renamed rather than dropped.
    'firm_name': 'Account.name',
    'website': 'Website.url',
    'live_url': 'Website.url',
    'stripe_subscription_id': 'Website.stripe_maintenance_subscription_id',
    'comp_package': 'split into comp_build/maintenance/social_tier',
    # The migration linkage itself.
    'client': 'Project.client is the legacy join being removed',
    'id': 'primary key',
    'created_at': 'TimestampedModel base',
    'updated_at': 'TimestampedModel base',
    'user': 'Account.user',
}


def _concrete_field_names(model):
    return {
        f.name for f in model._meta.get_fields()
        if getattr(f, 'concrete', False)
    }


class CanonicalColumnCoverageTests(SimpleTestCase):

    def _uncovered(self, legacy_model):
        from clients.account_models import Account, Website

        canonical = (_concrete_field_names(Account)
                     | _concrete_field_names(Website))
        return {
            name for name in _concrete_field_names(legacy_model)
            if name not in canonical
            and name not in DELIBERATELY_LEGACY_ONLY
        }

    def test_every_client_profile_field_has_a_canonical_home(self):
        from clients.models import ClientProfile

        uncovered = self._uncovered(ClientProfile)
        self.assertEqual(
            uncovered, set(),
            'ClientProfile fields with nowhere to go when the table is '
            f'dropped: {sorted(uncovered)}. Add the column to Account or '
            'Website and map it in refactor_to_accounts, or record it in '
            'DELIBERATELY_LEGACY_ONLY with the reason.')

    def test_every_project_field_has_a_canonical_home(self):
        from clients.models import Project

        uncovered = self._uncovered(Project)
        self.assertEqual(
            uncovered, set(),
            'Project fields with nowhere to go when the table is dropped: '
            f'{sorted(uncovered)}.')

    def test_the_six_recovered_fields_are_actually_present(self):
        """Named explicitly so a later refactor cannot quietly drop one
        and still satisfy the set-difference test above by adding it to
        the exemption dict."""
        from clients.account_models import Account, Website

        website_fields = _concrete_field_names(Website)
        for name in ('gbp_location_name', 'do_snapshot_id', 'site_status'):
            with self.subTest(field=name):
                self.assertIn(name, website_fields)

        account_fields = _concrete_field_names(Account)
        for name in ('payment_failure_started_at', 'payment_failure_offenses',
                     'stripe_social_subscription_id'):
            with self.subTest(field=name):
                self.assertIn(name, account_fields)

    def test_site_status_choices_match_the_legacy_state_machine(self):
        """The escalation tasks compare against these strings literally."""
        from clients.account_models import Website
        from clients.models import ClientProfile

        self.assertEqual(
            dict(Website.SITE_STATUS_CHOICES),
            dict(ClientProfile.SITE_STATUS_CHOICES))


class DefaultedFieldBackfillTests(SimpleTestCase):
    """A field whose default is a real value can never look empty."""

    def test_site_status_is_registered_as_defaulted(self):
        from clients.management.commands.refactor_to_accounts import (
            _DEFAULTED_FIELDS,
        )

        self.assertEqual(_DEFAULTED_FIELDS.get('site_status'), 'live')

    def test_a_defaulted_field_is_filled_from_legacy(self):
        """'live' on the canonical row means "never set", so a legacy
        'maintenance' must win. Otherwise a suspended site reads as
        healthy and the 503 has nothing scheduled to lift it."""
        from clients.management.commands.refactor_to_accounts import (
            _fill_missing,
        )

        instance = _FakeWebsite(site_status='live')
        changed = _fill_missing(instance, {'site_status': 'maintenance'})
        self.assertEqual(changed, ['site_status'])
        self.assertEqual(instance.site_status, 'maintenance')

    def test_a_deliberately_set_value_is_not_overwritten(self):
        """'offline' is not the default, so it is a real decision and a
        conflict for a human -- not something a backfill resolves."""
        from clients.management.commands.refactor_to_accounts import (
            _fill_missing,
        )

        instance = _FakeWebsite(site_status='offline')
        changed = _fill_missing(instance, {'site_status': 'maintenance'})
        self.assertEqual(changed, [])
        self.assertEqual(instance.site_status, 'offline')

    def test_an_identical_default_is_not_a_write(self):
        """Re-running the backfill must not bump updated_at, or every run
        looks like a change to the Moonieful staleness check."""
        from clients.management.commands.refactor_to_accounts import (
            _fill_missing,
        )

        instance = _FakeWebsite(site_status='live')
        self.assertEqual(_fill_missing(instance, {'site_status': 'live'}), [])
        self.assertEqual(instance.saved_fields, None)


class _FakeWebsite:
    """Minimal stand-in: _fill_missing only needs _meta.get_field and save."""

    def __init__(self, **values):
        self.__dict__.update(values)
        self.saved_fields = None

    class _Meta:
        @staticmethod
        def get_field(name):
            class _Field:
                is_relation = False
            return _Field()

    _meta = _Meta()

    def save(self, update_fields=None):
        self.saved_fields = update_fields
