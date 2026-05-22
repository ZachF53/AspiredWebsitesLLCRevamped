"""
TOTP (2FA) helpers for the vault — pyotp + QR generation.

TOTP is vault-level: one secret on the VaultConfig singleton, one
authenticator entry per admin, verified once per PIN session. Branded as
"Aspired Websites Servers" in the authenticator app.
"""

import base64
import io
import urllib.parse

import pyotp
import qrcode

from vault.crypto import decrypt_value

ISSUER_NAME = 'Aspired Websites Servers'
ACCOUNT_NAME = 'admin@aspiredwebsites.com'

# Apps that respect the otpauth `image` parameter (Aegis, 1Password, Authy,
# Raivo, …) will fetch this and show the Aspired favicon next to the entry.
# Google Authenticator silently ignores it — adding it is harmless either way.
FAVICON_URL = 'https://aspiredwebsites.com/static/images/favicon-32x32.png'


def generate_totp_secret():
    """Generate a new random base32 TOTP secret."""
    return pyotp.random_base32()


def get_totp_uri(secret):
    """
    The otpauth:// provisioning URI for the QR code. The entry shows as:

        Aspired Websites Servers
        admin@aspiredwebsites.com

    in every common authenticator app, with the Aspired favicon on those
    that support it.
    """
    base_uri = pyotp.TOTP(secret).provisioning_uri(
        name=ACCOUNT_NAME, issuer_name=ISSUER_NAME)
    image_param = '&image=' + urllib.parse.quote(FAVICON_URL, safe='')
    return base_uri + image_param


def generate_qr_code_base64(uri):
    """A base64-encoded PNG QR code — embedded inline, no file storage."""
    img = qrcode.make(uri)
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


def verify_totp_code(secret, code):
    """Verify a 6-digit TOTP code, allowing one 30s window of drift."""
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)


def get_decrypted_totp_secret(vault_config, vault_key):
    """Decrypt the vault-level TOTP secret with the PIN-derived vault key."""
    if not vault_config or not vault_config.totp_secret_encrypted:
        return None
    return decrypt_value(vault_config.totp_secret_encrypted, vault_key)
