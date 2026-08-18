"""
Transactional email must reach a client who has no legacy row.

Every send in `clients/emails.py` addressed `client.user.email`. `user`
lives on Account, so handing these the Website the caller actually holds
raised AttributeError — and it raised *during the send*, after the
report, invoice or stage transition it was announcing had already been
committed. The work was done and the client was never told.

`_display_name` immediately above those sends had already been made
canonical-safe, which is what made the gap easy to miss: the greeting
resolved fine and only the address did not.

The fixtures here are canonical-only — no ClientProfile anywhere — which
is both the post-drop world and the shape of every client created since
the cutover.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from clients.account_models import Account, Website

User = get_user_model()


class CanonicalRecipientTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='mailowner', email='owner@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=cls.user, name='Mail Co', contact_name='Dana Reyes')
        cls.website = Website.objects.create(
            account=cls.account, name='Mail Site', stage='design',
            staging_url='https://staging.example.com')

    def test_a_website_resolves_to_its_accounts_user(self):
        from clients.emails import _recipient

        self.assertEqual(_recipient(self.website), ['owner@example.com'])

    def test_an_account_resolves_directly(self):
        from clients.emails import _recipient

        self.assertEqual(_recipient(self.account), ['owner@example.com'])

    def test_no_address_on_file_sends_to_nobody_rather_than_raising(self):
        """A missing address is a data problem, not a reason to blow up
        the task that was trying to report progress."""
        from clients.emails import _recipient

        user = User.objects.create_user(
            username='noaddr', email='', password='x')
        account = Account.objects.create(user=user, name='No Address Co')
        site = Website.objects.create(account=account, name='No Address Site')

        self.assertEqual(_recipient(site), [])

    def test_email_alt_is_used_when_the_user_has_none(self):
        from clients.emails import _recipient

        user = User.objects.create_user(
            username='altonly', email='', password='x')
        account = Account.objects.create(
            user=user, name='Alt Co', email_alt='alt@example.com')
        site = Website.objects.create(account=account, name='Alt Site')

        self.assertEqual(_recipient(site), ['alt@example.com'])


class StageChangeEmailTests(TestCase):
    """The stage email is the client's only signal that work moved."""

    @classmethod
    def setUpTestData(cls):
        user = User.objects.create_user(
            username='stageowner', email='stage@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(user=user, name='Stage Co')
        cls.website = Website.objects.create(
            account=cls.account, name='Stage Site', stage='design',
            staging_url='https://staging.example.com')

    def test_it_addresses_a_website_without_raising(self):
        from clients.emails import send_stage_change_email

        with patch('clients.emails.send_branded') as sent:
            send_stage_change_email(self.website, 'design')

        sent.assert_called_once()
        self.assertEqual(
            sent.call_args.kwargs['recipient_list'], ['stage@example.com'])

    def test_the_review_email_carries_the_sites_staging_link(self):
        """Per site. Read off the account it would be whichever site's
        link happened to be found first."""
        from clients.emails import send_stage_change_email

        with patch('clients.emails.send_branded') as sent:
            send_stage_change_email(self.website, 'review')

        self.assertEqual(
            sent.call_args.kwargs['context']['staging_url'],
            'https://staging.example.com')

    def test_a_stage_with_no_copy_sends_nothing(self):
        from clients.emails import send_stage_change_email

        with patch('clients.emails.send_branded') as sent:
            send_stage_change_email(self.website, 'intake')
        sent.assert_not_called()

    def test_the_live_email_offers_maintenance_only_when_not_active(self):
        from clients.emails import send_stage_change_email

        with patch('clients.emails.send_branded') as sent:
            send_stage_change_email(self.website, 'live')
        self.assertTrue(
            sent.call_args.kwargs['context']['show_maintenance_upsell'])

        self.website.maintenance_active = True
        self.website.save(update_fields=['maintenance_active'])

        with patch('clients.emails.send_branded') as sent:
            send_stage_change_email(self.website, 'live')
        self.assertFalse(
            sent.call_args.kwargs['context']['show_maintenance_upsell'])
