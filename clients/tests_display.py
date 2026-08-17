"""
`__str__` must survive a null legacy client.

Every one of ~35 model `__str__` methods read `self.client.firm_name`
directly. `client` is nullable now and new rows leave it NULL, so each
would raise AttributeError -- in the admin changelist, in a `%s` log
line, and in the repr Django builds for an *unrelated* exception, which
is the worst of the three because it replaces the error someone was
actually trying to read.
"""

from django.test import SimpleTestCase, TestCase

from clients.display import UNASSIGNED, owner_label


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Named:
    def __init__(self, name):
        self.name = name


class _Profile:
    def __init__(self, firm_name):
        self.firm_name = firm_name


class OwnerLabelTests(SimpleTestCase):

    def test_the_site_wins_when_present(self):
        row = _Row(website_new=_Named('Vance Mediation'),
                   account_new=_Named('Vance Family Law'),
                   client=_Profile('Vance Family Law'))
        self.assertEqual(owner_label(row), 'Vance Mediation')

    def test_the_account_is_used_when_there_is_no_site(self):
        row = _Row(website_new=None, account_new=_Named('Vance Family Law'),
                   client=_Profile('Legacy Name'))
        self.assertEqual(owner_label(row), 'Vance Family Law')

    def test_the_legacy_profile_is_the_last_resort(self):
        row = _Row(website_new=None, account_new=None,
                   client=_Profile('Legacy Name'))
        self.assertEqual(owner_label(row), 'Legacy Name')

    def test_the_alternate_field_names_are_understood(self):
        """PaymentRecord uses `website`/`account`, not `_new`."""
        self.assertEqual(
            owner_label(_Row(website=_Named('Site'))), 'Site')
        self.assertEqual(
            owner_label(_Row(account=_Named('Acct'))), 'Acct')

    def test_a_row_owned_by_nothing_returns_a_placeholder(self):
        self.assertEqual(owner_label(_Row()), UNASSIGNED)
        self.assertEqual(
            owner_label(_Row(website_new=None, account_new=None,
                             client=None)),
            UNASSIGNED)

    def test_an_empty_name_falls_back_rather_than_rendering_blank(self):
        self.assertEqual(owner_label(_Row(website_new=_Named(''))),
                         UNASSIGNED)

    def test_a_broken_relation_does_not_raise(self):
        class _Exploding:
            @property
            def website_new(self):
                raise RuntimeError('related row is gone')

        self.assertEqual(owner_label(_Exploding()), UNASSIGNED)

    def test_the_legacy_project_hop_still_resolves(self):
        row = _Row(website_new=None, account_new=None, client=None,
                   project=_Row(client=_Profile('Via Project')))
        self.assertEqual(owner_label(row), 'Via Project')


class ModelStrWithNullClientTests(TestCase):
    """The real models, with the legacy FK left null the way every
    post-cutover write leaves it."""

    def _account_and_site(self):
        from django.contrib.auth import get_user_model

        from clients.models import ClientProfile

        user = get_user_model().objects.create_user(
            username='str-test', password='x', email='str@example.com')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Str Test LLC')
        account = profile.migrated_account
        return account, account.websites.first()

    def test_uptime_record_str_without_a_client(self):
        from clients.models import UptimeRecord

        _, site = self._account_and_site()
        row = UptimeRecord.objects.create(
            website_new=site, is_up=True, status_code=200)
        self.assertIn(site.name, str(row))

    def test_support_ticket_str_without_a_client(self):
        from clients.models import SupportTicket

        account, site = self._account_and_site()
        row = SupportTicket.objects.create(
            account_new=account, website_new=site,
            subject='Kettle broken', description='It is broken.')
        self.assertIn('Kettle broken', str(row))

    def test_monthly_report_str_without_a_client(self):
        import datetime

        from reporting.models import MonthlyReport

        _, site = self._account_and_site()
        row = MonthlyReport.objects.create(
            website_new=site, report_month=datetime.date(2026, 8, 1))
        self.assertIn(site.name, str(row))

    def test_a_row_owned_by_nothing_still_renders(self):
        """The shape the parity audit flags: no canonical owner, no
        legacy one. It must still be printable, or the admin page that
        would let someone fix it cannot render."""
        from clients.models import UptimeRecord

        row = UptimeRecord.objects.create(is_up=False, status_code=500)
        self.assertEqual(str(row).startswith(UNASSIGNED), True)
