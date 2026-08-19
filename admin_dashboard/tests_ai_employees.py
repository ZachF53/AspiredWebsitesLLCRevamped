"""Tests for the AI Employees cockpit (COLD_OUTREACH_AGENT.md §8.2/§8.3)."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from admin_dashboard.models import (
    AIEmployee,
    AIEmployeeAction,
    AIEmployeeRun,
    AIEmployeeTask,
)


class AIEmployeePageTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'zach', 'z@example.com', 'pw')
        self.client.force_login(self.admin)
        self.employee = AIEmployee.objects.get(slug='prospect')

    # ── list ──────────────────────────────────────────────────────────

    def test_list_page_renders_and_shows_prospect(self):
        r = self.client.get(reverse('admin_dashboard:ai_employees'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Prospect')
        self.assertContains(r, 'Paused')

    def test_list_page_states_runtime_is_not_built(self):
        """The page must not imply the agent works when it does not."""
        r = self.client.get(reverse('admin_dashboard:ai_employees'))
        self.assertContains(r, 'agent runtime is not built yet')

    def test_detail_page_renders(self):
        r = self.client.get(reverse(
            'admin_dashboard:ai_employee_detail', args=['prospect']))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'has never run')

    def test_detail_404s_for_unknown_slug(self):
        r = self.client.get(reverse(
            'admin_dashboard:ai_employee_detail', args=['nobody']))
        self.assertEqual(r.status_code, 404)

    def test_pages_require_admin(self):
        self.client.logout()
        r = self.client.get(reverse('admin_dashboard:ai_employees'))
        self.assertIn(r.status_code, (302, 403, 404))

    # ── controls that are real ────────────────────────────────────────

    def test_toggle_active_persists(self):
        self.assertFalse(self.employee.active)
        self.client.post(reverse(
            'admin_dashboard:ai_employee_toggle_active', args=['prospect']))
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.active)

        self.client.post(reverse(
            'admin_dashboard:ai_employee_toggle_active', args=['prospect']))
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.active)

    def test_task_is_queued_and_attributed(self):
        self.client.post(
            reverse('admin_dashboard:ai_employee_add_task', args=['prospect']),
            {'instruction': 'Focus on Round Rock personal injury firms.'})
        t = AIEmployeeTask.objects.get(employee=self.employee)
        self.assertEqual(t.status, 'pending')
        self.assertEqual(t.created_by, self.admin)

    def test_blank_task_is_rejected(self):
        self.client.post(
            reverse('admin_dashboard:ai_employee_add_task', args=['prospect']),
            {'instruction': '   '})
        self.assertEqual(AIEmployeeTask.objects.count(), 0)

    # ── the control that must NOT pretend to work ─────────────────────

    def test_wake_is_refused_server_side_while_runtime_missing(self):
        """Hiding the button in the template is not enough — a crafted
        POST must not be able to fabricate a run either."""
        r = self.client.post(
            reverse('admin_dashboard:ai_employee_wake', args=['prospect']),
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(AIEmployeeRun.objects.count(), 0)
        self.assertContains(r, 'Cannot wake')

    def test_wake_button_renders_disabled(self):
        r = self.client.get(reverse(
            'admin_dashboard:ai_employee_detail', args=['prospect']))
        self.assertContains(r, 'Disabled until the agent runtime exists')

    # ── run log ───────────────────────────────────────────────────────

    def test_run_and_actions_render(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='manual', status='completed',
            summary='Looked at 3 leads.', steps_used=4,
            spend_usd=Decimal('0.1234'))
        AIEmployeeAction.objects.create(
            run=run, tool_name='research_lead',
            tool_input={'lead_id': 7}, result='ok')

        r = self.client.get(reverse(
            'admin_dashboard:ai_employee_detail', args=['prospect']))
        self.assertContains(r, 'Looked at 3 leads.')
        self.assertContains(r, 'research_lead')

    def test_pending_action_can_be_approved(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled')
        action = AIEmployeeAction.objects.create(
            run=run, tool_name='propose_new_template_variant',
            tool_input={'name': 'Speed'}, requires_approval=True)

        self.client.post(
            reverse('admin_dashboard:ai_action_decide', args=[action.pk]),
            {'decision': 'approve'})
        action.refresh_from_db()
        self.assertTrue(action.approved)
        self.assertEqual(action.approved_by, self.admin)
        self.assertIsNotNone(action.approved_at)

    def test_pending_action_can_be_rejected(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled')
        action = AIEmployeeAction.objects.create(
            run=run, tool_name='queue_apify_search',
            tool_input={}, requires_approval=True)

        self.client.post(
            reverse('admin_dashboard:ai_action_decide', args=[action.pk]),
            {'decision': 'reject'})
        action.refresh_from_db()
        self.assertFalse(action.approved)

    def test_already_decided_action_cannot_be_decided_again(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled')
        action = AIEmployeeAction.objects.create(
            run=run, tool_name='x', requires_approval=True, approved=True)
        r = self.client.post(
            reverse('admin_dashboard:ai_action_decide', args=[action.pk]),
            {'decision': 'reject'})
        self.assertEqual(r.status_code, 404)


class AIEmployeeBadgeTests(TestCase):
    """§8.3 — the badge counts AIEmployeeAction ONLY."""

    def setUp(self):
        self.employee = AIEmployee.objects.get(slug='prospect')

    def _badge(self):
        from admin_dashboard.context import _admin_context
        return _admin_context()['ai_employees_pending_count']

    def test_zero_when_nothing_pending(self):
        self.assertEqual(self._badge(), 0)

    def test_counts_pending_actions(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled')
        AIEmployeeAction.objects.create(
            run=run, tool_name='a', requires_approval=True)
        AIEmployeeAction.objects.create(
            run=run, tool_name='b', requires_approval=True)
        self.assertEqual(self._badge(), 2)

    def test_decided_actions_do_not_count(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled')
        AIEmployeeAction.objects.create(
            run=run, tool_name='a', requires_approval=True, approved=True)
        self.assertEqual(self._badge(), 0)

    def test_actions_not_needing_approval_do_not_count(self):
        run = AIEmployeeRun.objects.create(
            employee=self.employee, trigger='scheduled')
        AIEmployeeAction.objects.create(
            run=run, tool_name='log_note', requires_approval=False)
        self.assertEqual(self._badge(), 0)

    def test_email_approvals_are_not_double_counted_here(self):
        """EmailSent.pending_approval belongs to the Approvals badge. If it
        leaked into this one, the operator would see the same queue twice
        and neither number could be trusted."""
        from outreach.models import EmailSent, Lead
        lead = Lead.objects.create(
            firm_name='X', email='x@example.com', city='Austin',
            state='Texas')
        EmailSent.objects.create(
            lead=lead, kind='cold', status='pending_approval',
            subject='s', body='b', from_email='z@example.com',
            sequence_step=1)

        from admin_dashboard.context import _admin_context
        ctx = _admin_context()
        self.assertEqual(ctx['approvals_count'], 1)
        self.assertEqual(ctx['ai_employees_pending_count'], 0)


class AIEmployeeNavTests(TestCase):

    def test_nav_item_present_and_resolves(self):
        from admin_dashboard.navigation import NAVIGATION
        items = [i for g in NAVIGATION for i in g.items
                 if i.label == 'AI Employees']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, '/admin-dashboard/ai-employees/')
        self.assertEqual(items[0].badge, 'ai_employees_pending_count')
