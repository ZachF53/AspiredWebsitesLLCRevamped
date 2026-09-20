"""
Public account-setup page (`onboarding_setup` view, /onboarding/setup/<token>/)
— the onboarding_status write on submit.

Regression for a real bug hit on staging: the view unconditionally wrote
`client.onboarding_status = 'pending_intake'` after the client set their
password + vault PIN. That's a valid state for the legacy ClientProfile
(pending_setup / pending_intake / onboarding_complete), but NOT for the new
Account model (pending_setup / complete only — per-website intake lives on
Website now). Writing an out-of-choices string meant:
  - The v2 accounts list rendered the raw slug ("pending_intake") instead
    of a display label, since get_onboarding_status_display() falls back
    to the stored value when it doesn't match any choice.
  - The v2 account detail page's "Onboarding status" <select> rendered
    with nothing selected (none of its options matched the stored value).
  - Account.onboarding_complete never got set to True, so the checkbox
    stayed unchecked despite the client having actually finished setup.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account
from clients.models import ClientProfile, OnboardingToken

User = get_user_model()

_POST_DATA = {
    'password': 'a-real-password1',
    'password_confirm': 'a-real-password1',
    'pin_1': '1', 'pin_2': '2', 'pin_3': '3', 'pin_4': '4',
    'pin_confirm_1': '1', 'pin_confirm_2': '2',
    'pin_confirm_3': '3', 'pin_confirm_4': '4',
}


class AccountOnboardingSetupTests(TestCase):

    def test_account_setup_marks_onboarding_status_complete(self):
        user = User.objects.create_user(
            username='setup_acct', email='setup_acct@example.com',
            password=None, is_active=False)
        account = Account.objects.create(
            user=user, name='Setup Co', onboarding_status='pending_setup')
        token = OnboardingToken.objects.create(account_new=account)

        r = self.client.post(
            reverse('onboarding_setup', args=[token.token]), _POST_DATA)
        self.assertEqual(r.status_code, 302)

        account.refresh_from_db()
        self.assertEqual(account.onboarding_status, 'complete')
        self.assertTrue(account.onboarding_complete)

    def test_account_setup_display_label_has_no_underscore(self):
        user = User.objects.create_user(
            username='setup_acct2', email='setup_acct2@example.com',
            password=None, is_active=False)
        account = Account.objects.create(
            user=user, name='Setup Co 2', onboarding_status='pending_setup')
        token = OnboardingToken.objects.create(account_new=account)

        self.client.post(
            reverse('onboarding_setup', args=[token.token]), _POST_DATA)

        account.refresh_from_db()
        self.assertEqual(account.get_onboarding_status_display(), 'Complete')

    def test_legacy_client_profile_setup_still_advances_to_pending_intake(self):
        """The legacy flow's own next step is its intake form —
        'pending_intake' is a real, valid state for ClientProfile and must
        keep working exactly as before."""
        user = User.objects.create_user(
            username='setup_legacy', email='setup_legacy@example.com',
            password=None, is_active=False)
        profile = ClientProfile.objects.create(
            user=user, firm_name='Legacy Firm',
            onboarding_status='pending_setup')
        token = OnboardingToken.objects.create(client=profile)

        r = self.client.post(
            reverse('onboarding_setup', args=[token.token]), _POST_DATA)
        self.assertEqual(r.status_code, 302)

        profile.refresh_from_db()
        self.assertEqual(profile.onboarding_status, 'pending_intake')
