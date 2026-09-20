"""clients.services.resolve_maintenance_onboarding /
admin_mark_maintenance_onboarding_complete."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from clients.account_models import Account, Website, WebsiteStageLog
from clients.service_models import MaintenancePlan
from clients.services import (
    admin_mark_maintenance_onboarding_complete,
    resolve_maintenance_onboarding,
)
from onboarding.models import Onboarding

User = get_user_model()

_seq = 0


def _account_and_website():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'miov{_seq}', email=f'miov{_seq}@example.com', password='x')
    account = Account.objects.create(user=u, name=f'MIOV Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'MIOV Site {_seq}', build_platform='custom')
    return account, website


class ResolveMaintenanceOnboardingTests(TestCase):

    def test_no_active_plan_returns_none_none(self):
        _, website = _account_and_website()
        plan, ob = resolve_maintenance_onboarding(website)
        self.assertIsNone(plan)
        self.assertIsNone(ob)

    def test_active_plan_no_onboarding_yet(self):
        account, website = _account_and_website()
        plan = MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        resolved_plan, ob = resolve_maintenance_onboarding(website)
        self.assertEqual(resolved_plan.id, plan.id)
        self.assertIsNone(ob)

    def test_matches_by_user_product_type_and_tier(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        ob = Onboarding.objects.create(
            user=account.user, product_type='maintenance',
            tier_slug='hvac-full-plan')
        _plan, resolved_ob = resolve_maintenance_onboarding(website)
        self.assertEqual(resolved_ob.id, ob.id)


class AdminMarkMaintenanceOnboardingCompleteTests(TestCase):

    def test_no_op_without_active_plan(self):
        _, website = _account_and_website()
        result = admin_mark_maintenance_onboarding_complete(
            website, set_by='Tester')
        self.assertIsNone(result)

    def test_no_op_without_onboarding_row(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        result = admin_mark_maintenance_onboarding_complete(
            website, set_by='Tester')
        self.assertIsNone(result)

    def test_marks_complete_and_logs(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        ob = Onboarding.objects.create(
            user=account.user, product_type='maintenance',
            tier_slug='hvac-full-plan')

        result = admin_mark_maintenance_onboarding_complete(
            website, set_by='Zach Long')
        self.assertEqual(result.id, ob.id)
        ob.refresh_from_db()
        self.assertIsNotNone(ob.completed_at)

        log = WebsiteStageLog.objects.filter(website=website).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.set_by, 'Zach Long')
        self.assertIn('admin override', log.note)

    def test_idempotent(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active')
        ob = Onboarding.objects.create(
            user=account.user, product_type='maintenance',
            tier_slug='hvac-full-plan')
        admin_mark_maintenance_onboarding_complete(website, set_by='Tester')
        ob.refresh_from_db()
        first_completed_at = ob.completed_at

        admin_mark_maintenance_onboarding_complete(website, set_by='Tester')
        ob.refresh_from_db()
        self.assertEqual(ob.completed_at, first_completed_at)
