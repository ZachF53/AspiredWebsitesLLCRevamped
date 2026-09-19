"""
v2 account setup-email flow — setup_complete derivation + on-demand token
minting.

Denis Law Group's Account is the motivating case: onboarding_status=
'complete' but onboarding_complete=False, User.is_active=True but
has_usable_password()=False, never logged in. She reads as fully set up
(old derivation) and simultaneously cannot log in — the setup-email button
never renders and, even if it did, the send view errors on a missing
token because tokens are only ever minted alongside invoice generation.

Three fixes under test:
  1. setup_complete now reads the User (active + usable password), not
     Account.onboarding_status.
  2. account_send_setup_email mints an OnboardingInvoice + OnboardingToken
     on demand when neither exists, instead of erroring.
  3. An account with no deliverable email address (found on "Moonieful
     Designs" — user.email='', a legacy pre-launch stub) is refused before
     anything is minted, instead of silently no-op-sending and still
     reporting success.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from admin_dashboard.v2.views_accounts import _account_is_set_up
from clients.account_models import Account
from clients.models import OnboardingInvoice, OnboardingToken

User = get_user_model()


def _staff_admin():
    return User.objects.create_superuser(
        username='v2admin', email='v2admin@example.com',
        password='test-pass-123')


class SetupCompleteDerivationTests(TestCase):
    """_account_is_set_up — the read-side fix."""

    def test_usable_password_reads_as_set_up(self):
        user = User.objects.create_user(
            username='readyclient', email='ready@example.com',
            password='a-real-password', is_active=True)
        account = Account.objects.create(
            user=user, name='Ready Co', onboarding_status='pending_setup')
        self.assertTrue(_account_is_set_up(account))

    def test_denis_shape_unusable_password_reads_as_not_set_up(self):
        """Unusable password + onboarding_status='complete' — Denis's
        exact shape. Must read as NOT set up despite the status field."""
        user = User.objects.create_user(
            username='denis', email='aisha@denislawgroup.com',
            password=None, is_active=True)
        self.assertFalse(user.has_usable_password())
        account = Account.objects.create(
            user=user, name='Denis Law Group',
            onboarding_status='complete', onboarding_complete=False)
        self.assertFalse(_account_is_set_up(account))

    def test_inactive_user_with_password_reads_as_not_set_up(self):
        user = User.objects.create_user(
            username='inactive', email='inactive@example.com',
            password='a-real-password', is_active=False)
        account = Account.objects.create(
            user=user, name='Inactive Co', onboarding_status='complete')
        self.assertFalse(_account_is_set_up(account))

    def test_last_login_does_not_affect_the_result(self):
        """A client who set a password but hasn't logged in yet is still
        set up — login timing is not part of the check."""
        user = User.objects.create_user(
            username='neverloggedin', email='never@example.com',
            password='a-real-password', is_active=True)
        self.assertIsNone(user.last_login)
        account = Account.objects.create(
            user=user, name='Never Co', onboarding_status='pending_setup')
        self.assertTrue(_account_is_set_up(account))


class AccountSendSetupEmailViewTests(TestCase):
    """account_send_setup_email — button rendering + on-demand minting."""

    def setUp(self):
        self.admin = _staff_admin()
        self.client.force_login(self.admin)

    def _denis_like_account(self):
        user = User.objects.create_user(
            username='denislaw', email='aisha@denislawgroup.com',
            password=None, is_active=True)
        return Account.objects.create(
            user=user, name='Denis Law Group',
            onboarding_status='complete', onboarding_complete=False)

    def test_button_renders_for_unusable_password_account(self):
        account = self._denis_like_account()
        response = self.client.get(
            reverse('admin_dashboard:v2_account_detail',
                    args=[account.id]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn('Account setup is complete.', content)
        self.assertIn('Send account setup email', content)

    def test_usable_password_account_shows_complete_no_button(self):
        user = User.objects.create_user(
            username='setclient', email='set@example.com',
            password='a-real-password', is_active=True)
        account = Account.objects.create(
            user=user, name='Set Co', onboarding_status='pending_setup')
        response = self.client.get(
            reverse('admin_dashboard:v2_account_detail',
                    args=[account.id]))
        content = response.content.decode()
        self.assertIn('Account setup is complete.', content)
        self.assertNotIn('Send account setup email', content)
        self.assertNotIn('Resend account setup email', content)

    @patch('clients.emails.send_onboarding_setup_email')
    def test_no_token_mints_exactly_one_token_and_invoice_and_sends_one_email(
            self, mock_send):
        account = self._denis_like_account()
        self.assertFalse(
            OnboardingToken.objects.filter(account_new=account).exists())
        self.assertFalse(
            OnboardingInvoice.objects.filter(account_new=account).exists())

        response = self.client.post(
            reverse('admin_dashboard:v2_account_send_setup_email',
                    args=[account.id]),
            follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OnboardingToken.objects.filter(account_new=account).count(), 1)
        self.assertEqual(
            OnboardingInvoice.objects.filter(account_new=account).count(), 1)
        invoice = OnboardingInvoice.objects.get(account_new=account)
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.total_amount, 0)
        mock_send.assert_called_once()

    @patch('clients.emails.send_onboarding_setup_email')
    def test_clicking_twice_mints_once_and_cooldown_blocks_second_send(
            self, mock_send):
        account = self._denis_like_account()
        url = reverse('admin_dashboard:v2_account_send_setup_email',
                       args=[account.id])

        first = self.client.post(url, follow=True)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(
            OnboardingToken.objects.filter(account_new=account).count(), 1)
        self.assertEqual(
            OnboardingInvoice.objects.filter(account_new=account).count(), 1)

        second = self.client.post(url, follow=True)
        content = second.content.decode()
        self.assertIn('already sent within the last 24 hours', content)
        # Still exactly one of each — the cooldown blocked the send before
        # a second mint could ever be reached, and the mint path itself
        # is a no-op once a token exists.
        self.assertEqual(
            OnboardingToken.objects.filter(account_new=account).count(), 1)
        self.assertEqual(
            OnboardingInvoice.objects.filter(account_new=account).count(), 1)
        self.assertEqual(mock_send.call_count, 1)

    @patch('clients.emails.send_onboarding_setup_email')
    def test_existing_unused_token_is_reused_not_reminted(self, mock_send):
        account = self._denis_like_account()
        existing = OnboardingToken.objects.create(account_new=account)

        response = self.client.post(
            reverse('admin_dashboard:v2_account_send_setup_email',
                    args=[account.id]),
            follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OnboardingToken.objects.filter(account_new=account).count(), 1)
        self.assertEqual(
            OnboardingToken.objects.get(account_new=account).id,
            existing.id)
        # No invoice was minted — get_or_create found the token already
        # there and never entered the `created` branch.
        self.assertFalse(
            OnboardingInvoice.objects.filter(account_new=account).exists())
        mock_send.assert_called_once()


class NoRecipientEmailGuardTests(TestCase):
    """The 'Moonieful Designs' shape — no deliverable address at all.

    Before this fix: _recipient() returns [] for a missing address,
    send_mail() with an empty recipient list is a documented no-op, and
    the view still minted a token + zero-dollar invoice and reported
    "Account setup email sent." A message that was never delivered looked
    identical, in the database and in the UI, to one that was.
    """

    def setUp(self):
        self.admin = _staff_admin()
        self.client.force_login(self.admin)

    def _no_email_account(self, **overrides):
        user = User.objects.create_user(
            username='legacy-no-email', email='', password=None,
            is_active=True)
        defaults = dict(
            user=user, name='No Email Co',
            onboarding_status='complete', onboarding_complete=False)
        defaults.update(overrides)
        return Account.objects.create(**defaults)

    def test_button_disabled_and_reason_shown_for_no_email_account(self):
        account = self._no_email_account()
        response = self.client.get(
            reverse('admin_dashboard:v2_account_detail',
                    args=[account.id]))
        content = response.content.decode()
        self.assertIn('No email address on file', content)
        self.assertIn('disabled', content)

    @patch('clients.emails.send_onboarding_setup_email')
    def test_crafted_post_is_refused_no_token_no_invoice(self, mock_send):
        """A POST straight to the endpoint, bypassing the disabled
        button in the template entirely — the server-side check must
        still refuse it."""
        account = self._no_email_account()

        response = self.client.post(
            reverse('admin_dashboard:v2_account_send_setup_email',
                    args=[account.id]),
            follow=True)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('no email address on file', content)
        self.assertFalse(
            OnboardingToken.objects.filter(account_new=account).exists())
        self.assertFalse(
            OnboardingInvoice.objects.filter(account_new=account).exists())
        mock_send.assert_not_called()

    @patch('clients.emails.send_onboarding_setup_email')
    def test_email_alt_fallback_counts_as_a_deliverable_address(
            self, mock_send):
        """owner_recipient() falls back to Account.email_alt when the
        User has none — that counts as usable and must NOT be blocked."""
        account = self._no_email_account(email_alt='billing@example.com')

        response = self.client.post(
            reverse('admin_dashboard:v2_account_send_setup_email',
                    args=[account.id]),
            follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OnboardingToken.objects.filter(account_new=account).count(), 1)
        self.assertEqual(
            OnboardingInvoice.objects.filter(account_new=account).count(), 1)
        mock_send.assert_called_once()

    @patch('clients.emails.send_onboarding_setup_email')
    def test_account_with_email_is_unaffected(self, mock_send):
        """Control case — item 7. A normal account with an email on the
        User mints and sends exactly as it did before this fix."""
        user = User.objects.create_user(
            username='hasmail', email='client@example.com',
            password=None, is_active=True)
        account = Account.objects.create(
            user=user, name='Has Email Co',
            onboarding_status='complete', onboarding_complete=False)

        response = self.client.get(
            reverse('admin_dashboard:v2_account_detail',
                    args=[account.id]))
        content = response.content.decode()
        self.assertNotIn('No email address on file', content)

        response = self.client.post(
            reverse('admin_dashboard:v2_account_send_setup_email',
                    args=[account.id]),
            follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OnboardingToken.objects.filter(account_new=account).count(), 1)
        self.assertEqual(
            OnboardingInvoice.objects.filter(account_new=account).count(), 1)
        mock_send.assert_called_once()
