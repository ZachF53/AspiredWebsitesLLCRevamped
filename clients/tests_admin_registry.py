"""
The Django admin must not reference the legacy FK path.

`manage.py check` validates `list_display` and `list_filter`, but it does
**not** resolve `search_fields`. A stale `client__firm_name` there passes
every check and every import, then raises FieldError the first time a
staff member types into the search box -- after the legacy column is gone.
That is the worst possible time to find out, so these tests actually run a
search against every registered ModelAdmin.
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase


class AdminSearchFieldTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_superuser(
            username='admin-registry', email='a@example.com', password='x')

    def _request(self):
        request = RequestFactory().get('/admin/', {'q': 'anything'})
        request.user = self.staff
        return request

    def test_every_registered_search_resolves(self):
        """Executing the search is the only way to prove the path exists."""
        for model, model_admin in admin.site._registry.items():
            if not getattr(model_admin, 'search_fields', None):
                continue
            with self.subTest(model=model._meta.label):
                queryset, _ = model_admin.get_search_results(
                    self._request(), model._default_manager.all(), 'anything')
                # Forcing evaluation is what compiles the joins.
                list(queryset[:1])

    def test_every_registered_changelist_renders(self):
        """Covers list_display callables and list_select_related paths."""
        for model, model_admin in admin.site._registry.items():
            with self.subTest(model=model._meta.label):
                changelist = model_admin.get_changelist_instance(
                    self._request())
                list(changelist.get_queryset(self._request())[:1])


class LegacyAdminRegistrationTests(TestCase):

    def test_the_legacy_models_are_no_longer_registered(self):
        """ClientProfile and Project were the only client models with an
        admin, which made the table being dropped look like the live one."""
        from clients.models import ClientProfile, Project

        registered = set(admin.site._registry)
        self.assertNotIn(ClientProfile, registered)
        self.assertNotIn(Project, registered)

    def test_the_canonical_models_are_registered(self):
        from clients.account_models import Account, Website

        registered = set(admin.site._registry)
        self.assertIn(Account, registered)
        self.assertIn(Website, registered)

    def test_no_admin_searches_through_the_legacy_foreign_key(self):
        for model, model_admin in admin.site._registry.items():
            for field in getattr(model_admin, 'search_fields', ()) or ():
                with self.subTest(model=model._meta.label, field=field):
                    self.assertFalse(
                        field.startswith('client__')
                        or field.startswith('project__')
                        or '__client__' in field
                        or '__project__' in field,
                        f'{model._meta.label}.search_fields still traverses '
                        f'the legacy FK: {field}')


class ContractPartyTests(TestCase):
    """The contract names the account, not the site."""

    def test_party_names_come_from_the_account(self):
        from clients.admin import _ContractParty

        class _Account:
            name = 'Vance Family Law'
            contact_name = 'Dana Vance'

        class _Site:
            name = 'Vance Mediation Services'
            account = _Account()

        party = _ContractParty(_Site())
        self.assertEqual(party.firm_name, 'Vance Family Law')
        self.assertEqual(party.contact_name, 'Dana Vance')

    def test_a_site_with_no_account_falls_back_to_its_own_name(self):
        from clients.admin import _ContractParty

        class _Site:
            name = 'Orphan Site'
            account = None

        party = _ContractParty(_Site())
        self.assertEqual(party.firm_name, 'Orphan Site')
        self.assertEqual(party.contact_name, '')
