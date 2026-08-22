"""
Email verification — the stage that did not exist, and the reason 416
cold emails produced zero replies.

WHAT WENT WRONG
---------------
Measured on prod 2026-08-22, across the 416 sends:

    info@                                   111
    hello@ / office@ / lawassistant@         24
    consumer gmail/yahoo/hotmail/aol         97
    scraped garbage (rohtopharmacy5@, ...)    -

Roughly 56% went to a mailbox no decision-maker reads. The cause is
upstream: Google Places returns a *business*, and a business has an
info@ inbox. ``enricher.py`` then scraped whatever email string appeared
on the homepage, which is where ``rohtopharmacy5@`` came from during a
law firm search.

TWO INDEPENDENT CHECKS
----------------------
1. **Role-address suppression** — free, needs no vendor, runs always.
   Pure string matching against the local part. A role address is never
   worth sending to: nobody with authority reads it, and role mailboxes
   are a well-known spam-trap pattern, so it damages sender reputation
   on the way to being ignored.

2. **Bounce verification** — needs a vendor API. Above a 3% bounce rate
   Google and Microsoft begin filtering the sending domain, and no
   amount of warming undoes it. At ~1,000 leads/month this costs under
   $4.

These are separate on purpose. Check 1 works today with no account and
catches the largest share of the damage. Check 2 can be switched on when
a key exists without changing any calling code.

FAILING CLOSED
--------------
Every ambiguous outcome resolves to "do not send". A wrongly-rejected
lead costs one lead. A wrongly-accepted lead costs sender reputation
shared across every future send from that domain. The asymmetry is not
close.
"""

import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Verification statuses ──────────────────────────────────────────────
# Stored on Lead.email_verification_status.

PENDING = 'pending'          # not yet checked
VALID = 'valid'              # deliverable, safe to send
INVALID = 'invalid'          # hard bounce guaranteed — never send
RISKY = 'risky'              # catch-all or unknown — not evidence of a mailbox
ROLE = 'role'                # role mailbox — nobody reads it
CONSUMER = 'consumer'        # personal gmail/yahoo — deliverable but flagged
UNVERIFIED = 'unverified'    # no provider configured; role check passed

STATUS_CHOICES = [
    (PENDING, 'Pending'),
    (VALID, 'Valid'),
    (INVALID, 'Invalid — will bounce'),
    (RISKY, 'Risky — catch-all or unknown'),
    (ROLE, 'Role address — suppressed'),
    (CONSUMER, 'Consumer mailbox'),
    (UNVERIFIED, 'Unverified — no provider configured'),
]


# ── Role addresses ─────────────────────────────────────────────────────
# The local parts that go to a shared inbox rather than a person. Every
# one of these appeared in, or is adjacent to, what prod actually sent.
#
# Matched against the WHOLE local part, case-insensitively, after
# stripping any +tag and collapsing separators. Substring matching would
# reject "sales.director@" and "administer@", which are real people.

ROLE_LOCAL_PARTS = frozenset({
    # generic business
    'info', 'information', 'contact', 'contactus', 'hello', 'hi', 'hey',
    'office', 'admin', 'administrator', 'administration', 'general',
    'mail', 'email', 'inbox', 'team', 'staff', 'company', 'main',
    'frontdesk', 'reception', 'receptionist', 'desk',
    # sales / marketing
    'sales', 'marketing', 'ads', 'advertising', 'press', 'media', 'pr',
    'partnerships', 'partner', 'bizdev', 'business',
    # support / service
    'support', 'help', 'helpdesk', 'service', 'customerservice',
    'customercare', 'care', 'inquiries', 'enquiries', 'inquiry',
    'enquiry', 'questions', 'ask',
    # finance / ops
    'billing', 'invoices', 'invoice', 'accounts', 'accounting',
    'accountspayable', 'ap', 'ar', 'payments', 'finance', 'orders',
    # people ops
    'hr', 'jobs', 'careers', 'recruiting', 'recruitment', 'hiring',
    'resumes', 'apply',
    # legal / compliance
    'legal', 'privacy', 'compliance', 'dpo', 'security', 'abuse',
    # technical — these are RFC-mandated or automated, never a prospect
    'postmaster', 'webmaster', 'hostmaster', 'noreply', 'no-reply',
    'donotreply', 'do-not-reply', 'bounce', 'bounces', 'mailer-daemon',
    'mailerdaemon', 'notifications', 'notification', 'alerts', 'alert',
    'system', 'root', 'daemon', 'automated', 'robot', 'bot',
    # vertical-specific shared inboxes seen in law / medical scrapes
    'intake', 'newclients', 'newclient', 'casemanager', 'paralegal',
    'lawassistant', 'assistant', 'secretary', 'clerk', 'scheduling',
    'schedule', 'appointments', 'appointment', 'newpatients',
    'newpatient', 'patients', 'frontoffice', 'referrals',
})

# Free/consumer mail providers. Not rejected by default — a solo
# practitioner on gmail is a real prospect — but flagged so campaigns can
# segment them, because they convert differently and complain more.
CONSUMER_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com',
    'rocketmail.com', 'hotmail.com', 'outlook.com', 'live.com',
    'msn.com', 'aol.com', 'icloud.com', 'me.com', 'mac.com',
    'protonmail.com', 'proton.me', 'gmx.com', 'gmx.net', 'mail.com',
    'yandex.com', 'zoho.com', 'inbox.com', 'fastmail.com',
    'comcast.net', 'verizon.net', 'att.net', 'sbcglobal.net',
    'bellsouth.net', 'cox.net', 'charter.net', 'earthlink.net',
    'juno.com', 'aim.com', 'mailinator.com',
})

# Disposable/throwaway domains — always invalid for outreach.
DISPOSABLE_DOMAINS = frozenset({
    'mailinator.com', 'guerrillamail.com', '10minutemail.com',
    'tempmail.com', 'temp-mail.org', 'throwawaymail.com',
    'yopmail.com', 'trashmail.com', 'sharklasers.com',
    'getnada.com', 'dispostable.com', 'maildrop.cc',
})

# RFC 5322 is far looser than this, but anything failing here is a scrape
# artifact rather than a real address we are wrongly rejecting.
EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')

_SEPARATORS = re.compile(r'[._\-\s]')


def split_email(email):
    """('sarah.chen+tag', 'firm.com') from 'Sarah.Chen+tag@Firm.com '.

    Returns (None, None) when the string is not a usable address.
    """
    addr = (email or '').strip().lower()
    if not addr or not EMAIL_RE.match(addr):
        return None, None
    local, _, domain = addr.rpartition('@')
    return local, domain


def normalise_local(local):
    """Strip +tags and separators so 'front.desk' matches 'frontdesk'.

    Done because role mailboxes are spelled inconsistently —
    ``front.desk@``, ``front-desk@`` and ``frontdesk@`` are one inbox.
    """
    base = (local or '').split('+', 1)[0]
    return _SEPARATORS.sub('', base)


def is_role_address(email):
    """True when the address goes to a shared inbox rather than a person.

    Matches the whole normalised local part, never a substring: a
    substring rule would reject ``sales.director@`` and ``administer@``,
    both of which are real people worth emailing.
    """
    local, _ = split_email(email)
    if local is None:
        return False
    normalised = normalise_local(local)
    if normalised in ROLE_LOCAL_PARTS:
        return True
    # Also catch the un-normalised spelling, since a few entries in the
    # set legitimately contain a hyphen ('no-reply').
    return local.split('+', 1)[0] in ROLE_LOCAL_PARTS


def is_consumer_address(email):
    """True for gmail/yahoo/etc — deliverable, but a personal mailbox."""
    _, domain = split_email(email)
    return domain in CONSUMER_DOMAINS if domain else False


def is_disposable_address(email):
    _, domain = split_email(email)
    return domain in DISPOSABLE_DOMAINS if domain else False


def screen(email):
    """Free local checks. No network, no cost, no API key required.

    Returns a status from the module constants. This is the check that
    would have stopped 135 of prod's 416 sends, and it needs no account
    to switch on.
    """
    local, domain = split_email(email)
    if local is None:
        return INVALID
    if is_disposable_address(email):
        return INVALID
    if is_role_address(email):
        return ROLE
    if domain in CONSUMER_DOMAINS:
        return CONSUMER
    return PENDING


# ── Vendor verification ────────────────────────────────────────────────

class VerificationError(Exception):
    """The provider could not be reached or returned something unusable."""


def _verify_millionverifier(email, api_key, timeout):
    """https://api.millionverifier.com/api/v3/

    Response carries both ``result`` and ``quality``; ``result`` is the
    specific one and is what we map.
    """
    r = requests.get(
        'https://api.millionverifier.com/api/v3/',
        params={'api': api_key, 'email': email, 'timeout': 10},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise VerificationError(
            f'MillionVerifier HTTP {r.status_code}: {r.text[:160]}')
    try:
        data = r.json()
    except ValueError:
        raise VerificationError(
            f'MillionVerifier returned non-JSON: {r.text[:160]}')

    if data.get('error'):
        raise VerificationError(f"MillionVerifier: {data['error']}")

    return {
        'ok': VALID,
        'catch_all': RISKY,
        'unknown': RISKY,
        'disposable': INVALID,
        'invalid': INVALID,
        # Anything unrecognised falls through to RISKY below — failing
        # closed, because an unknown verdict is not a green light.
    }.get(str(data.get('result', '')).lower(), RISKY)


def _verify_zerobounce(email, api_key, timeout):
    """https://api.zerobounce.net/v2/validate"""
    r = requests.get(
        'https://api.zerobounce.net/v2/validate',
        params={'api_key': api_key, 'email': email, 'ip_address': ''},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise VerificationError(
            f'ZeroBounce HTTP {r.status_code}: {r.text[:160]}')
    try:
        data = r.json()
    except ValueError:
        raise VerificationError(
            f'ZeroBounce returned non-JSON: {r.text[:160]}')

    if data.get('error'):
        raise VerificationError(f"ZeroBounce: {data['error']}")

    return {
        'valid': VALID,
        'invalid': INVALID,
        'catch-all': RISKY,
        'unknown': RISKY,
        'spamtrap': INVALID,     # sending here is actively harmful
        'abuse': INVALID,        # known complainer
        'do_not_mail': INVALID,
    }.get(str(data.get('status', '')).lower(), RISKY)


_PROVIDERS = {
    'millionverifier': _verify_millionverifier,
    'zerobounce': _verify_zerobounce,
}


def verify_email(email, timeout=20):
    """Full verification: free screen first, then the vendor if configured.

    The screen runs first deliberately — a role address should never cost
    an API credit, and role suppression must keep working on a server
    with no verification key at all.

    Returns a status constant. Never raises for a bad address; only a
    genuine provider outage raises VerificationError, so the caller can
    retry rather than permanently marking a good lead invalid.
    """
    local = screen(email)
    if local in (INVALID, ROLE):
        return local

    provider_name = (getattr(settings, 'EMAIL_VERIFY_PROVIDER', '') or '')
    provider_name = provider_name.strip().lower()
    api_key = getattr(settings, 'EMAIL_VERIFY_API_KEY', '') or ''

    if not provider_name or not api_key:
        # No vendor. The address passed the free checks, but "did not
        # look obviously wrong" is not the same as "the mailbox exists",
        # so it is reported as unverified rather than valid and
        # is_sendable() decides what that is worth.
        return CONSUMER if local == CONSUMER else UNVERIFIED

    fn = _PROVIDERS.get(provider_name)
    if fn is None:
        logger.error(
            'EMAIL_VERIFY_PROVIDER=%r is not one of %s — treating as '
            'unconfigured', provider_name, sorted(_PROVIDERS))
        return CONSUMER if local == CONSUMER else UNVERIFIED

    result = fn(email, api_key, timeout)

    # A consumer mailbox that the vendor confirms is deliverable is still
    # a consumer mailbox — keep the more specific label so campaigns can
    # segment on it.
    if result == VALID and local == CONSUMER:
        return CONSUMER
    return result


# ── The send decision ──────────────────────────────────────────────────

def is_sendable(status):
    """Whether a verification status clears a lead to enter a campaign.

    Deliberately conservative. The settings that loosen it exist so the
    loosening is an explicit, recorded decision rather than something
    that happens by drift.
    """
    if status in (INVALID, ROLE, PENDING):
        return False

    if status == VALID:
        return True

    if status == CONSUMER:
        # Deliverable and often a genuine sole practitioner.
        return True

    if status == RISKY:
        return bool(getattr(settings, 'EMAIL_VERIFY_ALLOW_CATCH_ALL', False))

    if status == UNVERIFIED:
        # No vendor configured. Sending is a knowing acceptance of bounce
        # risk, which is what EMAIL_VERIFY_REQUIRED=False means.
        return not getattr(settings, 'EMAIL_VERIFY_REQUIRED', True)

    return False


def rejection_reason(status):
    """Human-readable 'why is this lead not being emailed', for the UI."""
    return {
        INVALID: 'Address is malformed, disposable, or a known bad mailbox.',
        ROLE: 'Role mailbox (info@, office@, ...) — no decision-maker '
              'reads it and it harms sender reputation.',
        RISKY: 'Catch-all or unknown — the domain accepts everything at '
               'SMTP time and bounces later.',
        PENDING: 'Not verified yet.',
        UNVERIFIED: 'No verification provider configured, and '
                    'EMAIL_VERIFY_REQUIRED is on.',
    }.get(status, '')


def verify_lead(lead, save=True):
    """Verify a Lead's email and record the outcome on the row.

    Returns the status. Leaves ``PENDING`` untouched on provider outage
    so a retry can still succeed — a transient 500 at the vendor must not
    permanently mark a good prospect invalid.
    """
    from django.utils import timezone

    if not lead.email:
        status = INVALID
    else:
        try:
            status = verify_email(lead.email)
        except VerificationError:
            logger.exception(
                'verification provider failed for lead %s — leaving '
                'status unchanged for retry', lead.pk)
            return lead.email_verification_status

    lead.email_verification_status = status
    lead.email_verified_at = timezone.now()
    if save:
        lead.save(update_fields=[
            'email_verification_status', 'email_verified_at', 'updated_at'])
    return status
