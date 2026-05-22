"""TOTP (2FA) helpers for elevated SSH access — pyotp + QR generation."""

import base64
import io

import pyotp
import qrcode

from vault.crypto import decrypt_value


def generate_totp_secret():
    """Generate a new random base32 TOTP secret."""
    return pyotp.random_base32()


def get_totp_uri(secret, credential_label):
    """The otpauth:// provisioning URI for an authenticator-app QR code."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=credential_label, issuer_name='Aspired Websites SSH')


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


def get_decrypted_totp_secret(credential, vault_key):
    """Decrypt a credential's stored TOTP secret with the vault key."""
    if not credential.totp_secret_encrypted:
        return None
    return decrypt_value(credential.totp_secret_encrypted, vault_key)
