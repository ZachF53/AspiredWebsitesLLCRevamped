"""
clients.services.mark_intake_complete — the admin override every "mark
intake complete" entry point calls (v1's admin button, v2's admin
button, the AI assistant's "mark X intake complete" command).

Regression for a real gap: the function used to flip ONLY the
IntakeResponse, never Website.onboarding_status. CLAUDE.md's own
command-pattern spec for this ("Intake.completed = True, stage
unlocked") requires both — a client "unblocked" via the old version
was still bounced to /portal/intake/ by clients/decorators.py, which
gates on Website.onboarding_status == 'pending_intake', not on
IntakeResponse.completed.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from clients.account_models import Account, Website, WebsiteStageLog
from clients.models import IntakeResponse
from clients.services import mark_intake_complete

User = get_user_model()

_seq = 0


def _account_and_website(**website_kwargs):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'iov{_seq}', email=f'iov{_seq}@example.com', password='x')
    account = Account.objects.create(user=u, name=f'Intake Override Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'Site {_seq}', build_platform='custom',
        **website_kwargs)
    return account, website


class MarkIntakeCompleteTests(TestCase):

    def test_flips_website_onboarding_status(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        mark_intake_complete(website, set_by='Tester')
        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'intake_complete')

    def test_creates_and_completes_intake_response_when_none_exists(self):
        """A freshly created v2 website has no IntakeResponse row at all
        until the client visits /portal/intake/ — the override must
        create one rather than silently no-op."""
        _, website = _account_and_website(onboarding_status='pending_intake')
        self.assertFalse(IntakeResponse.objects.filter(
            website_new=website).exists())

        intake = mark_intake_complete(website, set_by='Tester')

        self.assertTrue(intake.completed)
        self.assertIsNotNone(intake.completed_at)
        self.assertTrue(IntakeResponse.objects.filter(
            website_new=website, completed=True).exists())

    def test_writes_audit_log_entry(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        mark_intake_complete(website, set_by='Zach Long')
        log = WebsiteStageLog.objects.filter(website=website).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.set_by, 'Zach Long')
        self.assertIn('admin override', log.note)

    def test_does_not_downgrade_an_already_advanced_website(self):
        _, website = _account_and_website(onboarding_status='complete')
        mark_intake_complete(website, set_by='Tester')
        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'complete')

    def test_idempotent_on_already_completed_intake(self):
        _, website = _account_and_website(onboarding_status='pending_intake')
        first = mark_intake_complete(website, set_by='Tester')
        first_completed_at = first.completed_at

        second = mark_intake_complete(website, set_by='Tester')
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.completed_at, first_completed_at)

    def test_unlocks_the_real_portal_gate(self):
        """The whole point: after the override, the decorator's own
        pending_intake check must read False — not just the
        IntakeResponse flag."""
        _, website = _account_and_website(onboarding_status='pending_intake')
        mark_intake_complete(website, set_by='Tester')
        website.refresh_from_db()
        self.assertNotEqual(website.onboarding_status, 'pending_intake')
