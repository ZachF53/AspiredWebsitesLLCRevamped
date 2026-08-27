"""
The custom-amount field on /admin-dashboard/billing/new-invoice/.

The field and the backend that reads it both existed, but the JS that
un-hides it lived in an inline <script>. /admin-dashboard/ serves
CSP_ADMIN_DASHBOARD, which inherits `script-src 'self'` from CSP_PUBLIC
and blocks inline blocks outright — so the script never ran, the row
stayed display:none, and a custom-priced invoice could not be entered
from the UI at all. Selecting "Custom amount" simply did nothing.
"""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

User = get_user_model()

URL = '/admin-dashboard/billing/new-invoice/'

# <script> with no attributes — i.e. an inline block, not src=.
INLINE_SCRIPT = re.compile(r'<script\s*>')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CustomAmountFieldIsReachable(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='invstaff', email='invstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def test_page_renders(self):
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_custom_option_and_field_are_present(self):
        html = self.client.get(URL).content.decode()
        self.assertIn('value="custom"', html)
        self.assertIn('name="custom_amount"', html)
        self.assertIn('id="custom-amount-row"', html)

    def test_toggle_script_is_external(self):
        """An inline block here is dead code — CSP won't run it."""
        html = self.client.get(URL).content.decode()
        self.assertIn('js/invoice_custom_amount.js', html)

    def test_no_inline_script_block_on_this_page(self):
        html = self.client.get(URL).content.decode()
        self.assertIsNone(
            INLINE_SCRIPT.search(html),
            'inline <script> on an admin-dashboard page never executes — '
            'CSP_ADMIN_DASHBOARD sets script-src \'self\'. Move it to '
            'core/static/js/ and load it with {% static %}.')

    def test_admin_dashboard_csp_still_blocks_inline_script(self):
        """Pins the constraint the fix is built on."""
        csp = self.client.get(URL)['Content-Security-Policy']
        script_src = csp.split('script-src')[1].split(';')[0]
        self.assertNotIn("'unsafe-inline'", script_src)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CustomAmountValidation(TestCase):
    """The backend half — unchanged, but nothing covered it."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='invstaff2', email='invstaff2@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def _post(self, **over):
        data = {
            'first_name': 'Pat', 'last_name': 'Friend',
            'email': 'friend@example.com',
            'package': 'custom', 'custom_amount': '750',
        }
        data.update(over)
        return self.client.post(URL, data)

    def test_zero_is_rejected(self):
        resp = self._post(custom_amount='0')
        self.assertEqual(resp.status_code, 200)
        msgs = [str(m) for m in resp.context['messages']]
        self.assertIn('Custom amount must be a positive number.', msgs)

    def test_non_numeric_is_rejected(self):
        resp = self._post(custom_amount='seven fifty')
        self.assertEqual(resp.status_code, 200)
        msgs = [str(m) for m in resp.context['messages']]
        self.assertIn('Custom amount must be a positive number.', msgs)

    def test_blank_is_rejected(self):
        resp = self._post(custom_amount='')
        self.assertEqual(resp.status_code, 200)
        msgs = [str(m) for m in resp.context['messages']]
        self.assertIn('Custom amount must be a positive number.', msgs)
