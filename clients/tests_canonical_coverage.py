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


class CanonicalIndexCoverageTests(SimpleTestCase):
    """A composite index is as load-bearing as a column, and just as
    invisible to the parity audit.

    Twelve composite indexes were keyed on `client` / `project`. Once the
    readers moved to `website_new` / `account_new`, every one of them
    indexed a column nothing queried, and every query that replaced them
    ran with only the single-column FK index behind it -- no help at all
    for the ordering half of `(owner, timestamp)`.

    `clients.UptimeRecord` is the sharp edge: ~75k rows, and
    `get_uptime_chart_data` issues 90 `(website_new, checked_at)` queries
    per call. Nothing fails, so nothing reports it; the dashboard just
    gets slower every month.
    """

    #: Legacy owner columns being removed by the drop.
    LEGACY = frozenset({'client', 'project'})
    #: The canonical column that replaces each.
    CANONICAL = {'client': ('website_new', 'account_new'),
                 'project': ('website_new',)}

    @staticmethod
    def _plain(fields):
        """Index field list with sort markers stripped, for comparison."""
        return tuple(f.lstrip('-') for f in fields)

    def _missing_mirrors(self):
        from django.apps import apps

        missing = []
        for model in apps.get_models():
            declared = model._meta.indexes
            canonical_shapes = {self._plain(i.fields) for i in declared}
            for index in declared:
                shape = self._plain(index.fields)
                if not (self.LEGACY & set(shape)):
                    continue
                owner = shape[0]
                if owner not in self.LEGACY:
                    continue
                # A mirror is the same index with the owner swapped for
                # whichever canonical FK this model actually carries.
                fields = {f.name for f in model._meta.get_fields()}
                mirrored = False
                for replacement in self.CANONICAL[owner]:
                    if replacement not in fields:
                        continue
                    if (replacement,) + shape[1:] in canonical_shapes:
                        mirrored = True
                if not mirrored:
                    missing.append(f'{model._meta.label}{list(index.fields)}')
        return missing

    def test_every_legacy_composite_index_has_a_canonical_mirror(self):
        missing = self._missing_mirrors()
        self.assertEqual(
            missing, [],
            'These composite indexes are keyed on a legacy owner column '
            'with no equivalent on the canonical one, so the queries that '
            f'replaced them run unindexed: {missing}. Add the mirrored '
            'models.Index and a migration for it.')


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
