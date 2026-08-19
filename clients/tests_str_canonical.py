"""
Every model must survive `str()` with its legacy FK null.

`__str__` raising is the failure mode `clients.display` was written for,
and it is nasty precisely because it is not where anyone looks: it breaks
the Django admin changelist, the `%s` in a log line, and the repr inside
the exception message for the *original* problem someone was debugging.
Roughly thirty-five of them were once written as
`f'{self.client.firm_name} — ...'`.

`client` is nullable now and every write since the cutover leaves it
NULL, so this is not a hypothetical: it is the state of all new rows.

Rather than scan for the shape — which missed twenty-eight of them,
because `x.client.firm_name` names nothing legacy — this walks the model
registry and calls `str()` on a real instance of every model that has a
legacy owner FK, with only the canonical one set. There is nothing to
keep in sync: a model added later is covered the day it is added.
"""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Account, Website

User = get_user_model()


def _models_with_a_legacy_owner():
    """Every model carrying a `client` or `project` FK to a legacy model."""
    from clients.models import ClientProfile, Project

    found = []
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if not getattr(field, 'concrete', False):
                continue
            if getattr(field, 'related_model', None) not in (
                    ClientProfile, Project):
                continue
            if field.name in ('client', 'project'):
                found.append((model, field.name))
                break
    return found


class LegacyNullStrTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        user = User.objects.create_user(
            username='strowner', email='strowner@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(user=user, name='Str Owner Co')
        cls.website = Website.objects.create(
            account=cls.account, name='Str Owner Site')

    def _canonical_kwargs(self, model):
        """Set whichever canonical owner FK the model carries."""
        names = {f.name for f in model._meta.get_fields()
                 if getattr(f, 'concrete', False)}
        kwargs = {}
        if 'website_new' in names:
            kwargs['website_new'] = self.website
        if 'account_new' in names:
            kwargs['account_new'] = self.account
        return kwargs

    def _fill(self, model, kwargs):
        """Populate every non-relation field `__str__` might touch.

        This is what makes the differential below mean anything. Without
        it, a `__str__` that reads both the legacy FK *and* an unset date
        raises either way, gets written off as a fixture gap, and the
        model is silently skipped — which is exactly what happened on the
        first version of this test: reverting `ConversionEvent.__str__`
        to `self.client.firm_name` did not fail it, because
        `event_timestamp` was unset and masked the difference.
        """
        import datetime
        import decimal
        import uuid as _uuid

        for field in model._meta.get_fields():
            if not getattr(field, 'concrete', False) or field.is_relation:
                continue
            if field.name in kwargs or field.primary_key:
                continue
            if field.has_default() or field.null or field.blank:
                # Still fill nullable dates/numbers: `__str__` formats
                # them, and None is what breaks the format, not the FK.
                if not isinstance(field, (
                        models.DateField, models.DateTimeField,
                        models.DecimalField, models.IntegerField,
                        models.FloatField)):
                    continue
                if field.has_default():
                    continue

            if isinstance(field, models.DateTimeField):
                kwargs[field.name] = timezone.now()
            elif isinstance(field, models.DateField):
                kwargs[field.name] = datetime.date.today()
            elif isinstance(field, models.DecimalField):
                kwargs[field.name] = decimal.Decimal('1.00')
            elif isinstance(field, (models.IntegerField, models.FloatField)):
                kwargs[field.name] = 1
            elif isinstance(field, models.BooleanField):
                kwargs[field.name] = False
            elif isinstance(field, models.UUIDField):
                kwargs[field.name] = _uuid.uuid4()
            elif isinstance(field, models.JSONField):
                kwargs[field.name] = {}
            elif isinstance(field, (models.CharField, models.TextField)):
                kwargs[field.name] = 'x'
        return kwargs

    def _str_or_error(self, model, kwargs):
        try:
            return str(model(**kwargs)), None
        except Exception as exc:                          # noqa: BLE001
            return None, f'{type(exc).__name__}: {exc}'

    def test_a_null_legacy_owner_never_breaks_str(self):
        """Differential, not absolute.

        Building an unsaved instance of every model means most of them
        are missing a required date, decimal or FK that `__str__` also
        touches — and a first pass at this test reported ten "failures"
        that were all my fixture, not the models. Asserting "str() must
        not raise" measures how thoroughly the fixture is populated.

        So each model is stringified twice: once with the legacy FK null
        (the state of every row written since the cutover) and once with
        it pointing at a real ClientProfile. Only a model that works with
        it and breaks without it is a finding. Anything failing both ways
        is the fixture, and is ignored.
        """
        from clients.models import ClientProfile, Project

        profile = ClientProfile.objects.create(
            user=User.objects.create_user(
                username='strlegacy', email='strlegacy@example.com',
                password='test-pass-123'),
            firm_name='Str Legacy Firm')
        project = Project.objects.create(client=profile)

        checked = 0
        regressions = []
        fixture_gaps = 0

        for model, legacy_field in _models_with_a_legacy_owner():
            kwargs = self._canonical_kwargs(model)
            if not kwargs:
                continue
            kwargs = self._fill(model, kwargs)

            checked += 1
            without, err_without = self._str_or_error(model, kwargs)

            legacy = project if legacy_field == 'project' else profile
            with_legacy, err_with = self._str_or_error(
                model, {**kwargs, legacy_field: legacy})

            if err_without and not err_with:
                regressions.append(
                    f'{model._meta.label}.__str__ works with {legacy_field} '
                    f'set and raises without it — {err_without}')
            elif err_without and err_with:
                fixture_gaps += 1

        self.assertGreater(
            checked, 20,
            'the registry walk found almost nothing — the discovery is '
            'broken, not the models')
        # Coverage, asserted. A model that raises with the legacy FK set
        # *and* without it is skipped, and a test that silently skips
        # most of the registry passes while proving nothing — which is
        # what the first version of this did.
        self.assertLess(
            fixture_gaps, checked // 4,
            f'{fixture_gaps} of {checked} models could not be stringified '
            'even with the legacy FK set, so they are being skipped rather '
            'than checked. Fill the fields their __str__ reads.')
        self.assertEqual(
            regressions, [],
            'These __str__ methods depend on the legacy FK, which is NULL '
            'on every row written since the cutover. A raising __str__ '
            'breaks the admin changelist, the %s in a log line, and the '
            'repr inside the exception for whatever you were actually '
            f'debugging: {regressions}')

    def test_the_walk_finds_the_models_it_should(self):
        """Guards the test above from passing on an empty list."""
        labels = {m._meta.label for m, _ in _models_with_a_legacy_owner()}

        for expected in ('clients.SupportTicket', 'reporting.MonthlyReport',
                         'vault.ClientVault', 'social.ScheduledPost'):
            self.assertIn(expected, labels)
