"""
`__str__` must survive a null legacy client.

Every one of ~35 model `__str__` methods read `self.client.firm_name`
directly. `client` is nullable now and new rows leave it NULL, so each
would raise AttributeError -- in the admin changelist, in a `%s` log
line, and in the repr Django builds for an *unrelated* exception, which
is the worst of the three because it replaces the error someone was
actually trying to read.
"""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from clients.display import UNASSIGNED, owner_label
from clients.models import ClientProfile

User = get_user_model()


class _Row:
    """Stand-in for a model row.

    Sets the ``<attr>_id`` shadow Django gives every forward relation,
    because that shadow is exactly how `owner_label` tells a real FK from
    a same-named plain field — `ClientProfile.website` is a URL string,
    not a Website.
    """

    RELATIONS = ('website_new', 'website', 'account_new', 'account',
                 'client', 'project')

    def __init__(self, **kw):
        self.__dict__.update(kw)
        for name in self.RELATIONS:
            if name in kw:
                value = kw[name]
                self.__dict__[f'{name}_id'] = (
                    id(value) if value is not None else None)


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

    def test_a_same_named_plain_field_is_not_mistaken_for_a_relation(self):
        """ClientProfile.website is a URLField holding the live URL, not a
        Website FK. Reading it as a relation handed back a `str`, and the
        caller then asked that string for `.name` — which broke every
        contract-ready email.

        `owner_label` names the OWNER of a row, so a bare profile has no
        owner and correctly returns the placeholder. What must never
        happen is a crash, or the URL being passed off as a name.
        """

        class _LegacyProfile:
            website = 'https://client.example'   # a URLField, not an FK
            firm_name = 'Legacy Co'

        label = owner_label(_LegacyProfile())
        self.assertEqual(label, UNASSIGNED)
        self.assertNotIn('client.example', label)

    def test_a_row_owned_by_a_profile_with_a_url_still_names_the_client(self):
        """The shape that actually broke: a Contract whose `client` is a
        legacy profile carrying a URL in `website`."""
        row = _Row(client=_Row(firm_name='Legacy Co',
                               website='https://client.example'))
        self.assertEqual(owner_label(row), 'Legacy Co')

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


class EmailDisplayNameTests(SimpleTestCase):
    """`clients.emails._display_name` is what resolves the name an email
    is addressed to. It has to work for an Account, a Website and a legacy
    profile, because all three reach it during the cutover."""

    def test_an_account_contact_name_wins(self):
        from clients.emails import _display_name

        class _Account:
            contact_name = 'Dana Vance'
            name = 'Vance Family Law'

        self.assertEqual(_display_name(_Account()), 'Dana Vance')

    def test_an_account_with_no_contact_falls_back_to_its_name(self):
        """`firm_name` does not exist on Account. Reading it raised
        AttributeError mid-send the moment contact_name was blank."""
        from clients.emails import _display_name

        class _Account:
            contact_name = ''
            name = 'Vance Family Law'

        self.assertEqual(_display_name(_Account()), 'Vance Family Law')

    def test_a_legacy_profile_still_resolves(self):
        from clients.emails import _display_name

        class _Profile:
            contact_name = ''
            firm_name = 'Legacy Co'
            website = 'https://client.example'

        self.assertEqual(_display_name(_Profile()), 'Legacy Co')

    def test_nothing_resolvable_returns_empty_not_a_crash(self):
        from clients.emails import _display_name, _first_name

        class _Bare:
            pass

        self.assertEqual(_display_name(_Bare()), '')
        self.assertEqual(_first_name(_Bare()), 'there')


class RealInstancePassedDirectlyTests(TestCase):
    """The resolvers are handed an Account or a Website *itself*, not a
    row owned by one — the onboarding sweeps iterate those and pass them
    straight in.

    This gap survived every synthetic test above, because those build
    stand-in rows and never a real Account. It was found on staging:
    `owner_recipient(account)` returned ('', '') for an account whose user
    had a perfectly good email, which would have silently skipped every
    onboarding reminder for a client who had already paid.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        from clients.models import ClientProfile

        user = get_user_model().objects.create_user(
            username='direct', password='x', email='direct@example.com')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Direct Co', contact_name='Dana Direct')
        self.account = self.profile.migrated_account
        self.site = self.account.websites.first()

    def test_an_account_names_itself(self):
        from clients.display import owner_label

        self.assertEqual(owner_label(self.account), 'Direct Co')

    def test_a_website_names_itself(self):
        from clients.display import owner_label

        self.assertEqual(owner_label(self.site), self.site.name)

    def test_an_account_resolves_its_own_recipient(self):
        from clients.display import owner_recipient

        email, name = owner_recipient(self.account)
        self.assertEqual(email, 'direct@example.com')
        self.assertEqual(name, 'Dana Direct')

    def test_a_website_resolves_through_its_account(self):
        from clients.display import owner_recipient

        email, name = owner_recipient(self.site)
        self.assertEqual(email, 'direct@example.com')
        self.assertEqual(name, 'Dana Direct')

    def test_owner_account_of_an_account_is_itself(self):
        from clients.display import owner_account

        self.assertEqual(owner_account(self.account), self.account)

    def test_owner_account_of_a_website_is_its_account(self):
        from clients.display import owner_account

        self.assertEqual(owner_account(self.site), self.account)

    def test_the_onboarding_helpers_resolve_a_real_account(self):
        """The two that were actually broken."""
        from clients.tasks import _recipient_email, _setup_first_name

        self.assertEqual(
            _recipient_email(self.account), 'direct@example.com')
        self.assertEqual(_setup_first_name(self.account), 'Dana')

    def test_the_onboarding_helpers_resolve_a_real_website(self):
        from clients.tasks import _recipient_email, _setup_first_name

        self.assertEqual(_recipient_email(self.site), 'direct@example.com')
        self.assertEqual(_setup_first_name(self.site), 'Dana')


class LegacyProfileOwnerTests(TestCase):
    """A ClientProfile handed to the resolvers directly.

    Not hypothetical: droplet provisioning receives one, and
    `owner_account` fell through every branch and returned None — so the
    new server's SSH credential was written into a vault keyed on
    nothing, minutes after the customer paid. The Account and Website
    branches above were added for exactly this shape of bug; this is the
    third instance of it.
    """

    def setUp(self):
        user = User.objects.create_user(
            username='legacyowner', email='legacyowner@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Legacy Owner Co')

    def test_owner_account_resolves_the_migrated_account(self):
        from clients.display import owner_account

        self.assertEqual(
            owner_account(self.profile), self.profile.migrated_account)

    def test_owner_recipient_reaches_the_user(self):
        from clients.display import owner_recipient

        email, _name = owner_recipient(self.profile)
        self.assertEqual(email, 'legacyowner@example.com')

    def test_owner_label_names_it(self):
        from clients.display import owner_label

        self.assertIn('Legacy Owner Co', owner_label(self.profile))
