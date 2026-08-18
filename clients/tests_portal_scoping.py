"""
Portal ownership scoping.

Wave 1 made `client_required` admit a user who holds an Account but no
legacy ClientProfile — the shape every client created after the cutover
will have. Four portal views and one access-control check had not caught
up, and both failure modes are serious:

  * the list views scoped with `{'client': request.client_profile}`,
    which for an Account-only client becomes `{'client': None}` — not
    "their rows", but "rows owned by nobody";
  * the suggestion ownership check read `request.client_profile.id` as
    the first term of an `or`, so it raised AttributeError before the
    canonical branch could answer.
"""

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from clients.account_models import Account, Website
from clients.models import ClientProfile
from clients.views import _owner_filter, _owns


User = get_user_model()


class OwnerFilterTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        user = User.objects.create_user(
            username='scoping', email='scoping@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Scoping Firm')
        self.account = self.profile.migrated_account
        self.website = self.account.websites.get()

    def _request(self, **attrs):
        request = self.factory.get('/portal/')
        for key, value in attrs.items():
            setattr(request, key, value)
        return request

    def test_a_picked_website_scopes_to_that_website(self):
        request = self._request(
            website=self.website, account=self.account,
            client_profile=self.profile)
        self.assertEqual(_owner_filter(request), {'website_new': self.website})

    def test_account_only_client_is_scoped_to_their_account(self):
        """The bug: this used to produce {'client': None}."""
        request = self._request(
            website=None, account=self.account, client_profile=None)

        flt = _owner_filter(request)
        self.assertEqual(flt, {'website_new__account': self.account})
        self.assertNotIn('client', flt)

    def test_legacy_only_client_still_works(self):
        request = self._request(
            website=None, account=None, client_profile=self.profile)
        self.assertEqual(_owner_filter(request), {'client': self.profile})

    def test_an_unowned_request_matches_nothing(self):
        """Fail closed. An unscoped filter on a portal page shows one
        client another client's records."""
        from clients.models import SupportTicket

        SupportTicket.objects.create(
            client=self.profile, subject='Someone elses', description='x')

        request = self._request(
            website=None, account=None, client_profile=None)
        flt = _owner_filter(request)

        self.assertEqual(SupportTicket.objects.filter(**flt).count(), 0)
        self.assertNotEqual(flt, {})


class OwnershipCheckTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        user = User.objects.create_user(
            username='owns', email='owns@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Owns Firm')
        self.account = self.profile.migrated_account
        self.website = self.account.websites.get()

        other_user = User.objects.create_user(
            username='owns-other', email='owns-other@example.com',
            password='test-pass-123')
        self.other_profile = ClientProfile.objects.create(
            user=other_user, firm_name='Other Firm')
        self.other_account = self.other_profile.migrated_account

    def _request(self, **attrs):
        request = self.factory.get('/portal/')
        for key, value in attrs.items():
            setattr(request, key, value)
        return request

    def _suggestion(self, **kwargs):
        from clients.models import IntelligenceSuggestion

        return IntelligenceSuggestion.objects.create(
            title='A recommendation', **kwargs)

    def test_account_only_client_does_not_crash_the_check(self):
        """This raised AttributeError on an access-control path."""
        suggestion = self._suggestion(
            client=self.profile, website_new=self.website)
        request = self._request(
            account=self.account, client_profile=None)

        self.assertTrue(_owns(request, suggestion))

    def test_owner_is_recognised_through_the_website(self):
        suggestion = self._suggestion(
            client=self.profile, website_new=self.website)
        request = self._request(
            account=self.account, client_profile=self.profile)
        self.assertTrue(_owns(request, suggestion))

    def test_another_clients_row_is_refused(self):
        other_site = Website.objects.create(
            account=self.other_account, name='Other Site')
        suggestion = self._suggestion(
            client=self.other_profile, website_new=other_site)

        request = self._request(
            account=self.account, client_profile=self.profile)
        self.assertFalse(_owns(request, suggestion))

    def test_refused_when_the_request_owns_nothing(self):
        suggestion = self._suggestion(
            client=self.profile, website_new=self.website)
        request = self._request(account=None, client_profile=None)
        self.assertFalse(_owns(request, suggestion))

    def test_a_legacy_only_row_is_refused(self):
        """The last legacy branch is gone, and this is the deliberate
        consequence.

        `_owns` used to fall back to `request.client_profile` when the
        row matched neither the site nor the account. The portal no
        longer resolves a legacy profile at all — the decorator does not
        attach one — so that branch could only ever compare against None.

        A row owned solely by a legacy profile is therefore refused. That
        is the correct direction for an ownership check: refusing a row
        the request cannot prove it owns shows an empty page, while
        guessing hands one client another client's data. Any such row is
        also a parity finding, and the audit reports zero of them.
        """
        suggestion = self._suggestion(client=self.profile)
        request = self._request(account=None, client_profile=self.profile)
        self.assertFalse(_owns(request, suggestion))
