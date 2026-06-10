"""
Phase 5b/5c — Social tests. The Phase 5a tests covered GBP; that code
moved to reporting/ where the GBP-as-maintenance feature lives now.

The crypto + SocialToken + ScheduledPost + PostResult models stay
here — they're still the right shape for Meta + LinkedIn in 5b/5c.
"""

from django.test import TestCase, override_settings

from social.crypto import decrypt_token, encrypt_token


TEST_SETTINGS = {
    'VAULT_SERVER_SECRET': 'test-vault-server-secret-for-social-tests',
}


@override_settings(**TEST_SETTINGS)
class CryptoRoundTripTests(TestCase):
    """The encrypt/decrypt wrappers — server-key encryption round-trip
    + the no-oracle decrypt failure path."""

    def test_encrypt_decrypt_roundtrip(self):
        plaintext = 'access_token_test_value'
        cipher = encrypt_token(plaintext)
        self.assertNotIn(plaintext, cipher)
        self.assertEqual(decrypt_token(cipher), plaintext)

    def test_empty_input_returns_empty(self):
        self.assertEqual(encrypt_token(''), '')
        self.assertEqual(decrypt_token(''), '')

    def test_decrypt_garbage_returns_empty(self):
        # No oracle — bad ciphertext returns '' not an exception.
        self.assertEqual(decrypt_token('not-valid-hex'), '')

    @override_settings(VAULT_SERVER_SECRET='')
    def test_encrypt_raises_friendly_error_without_secret(self):
        from social.crypto import encrypt_token
        with self.assertRaises(RuntimeError) as cm:
            encrypt_token('foo')
        self.assertIn('VAULT_SERVER_SECRET', str(cm.exception))
