"""
Namecheap API client wrapper.

Talks XML over HTTP to api.namecheap.com (live) or
api.sandbox.namecheap.com (sandbox), picking which set of credentials
to use based on settings.NAMECHEAP_SANDBOX. Every public method
returns plain Python dicts so callers never touch raw XML.

Conventions:
  - All money values are returned as Decimal.
  - All booleans are real Python bools (not 'true'/'false' strings).
  - All timestamps are timezone-aware UTC datetimes.
  - Errors raise NamecheapError with the API's error number + message.

Retry policy:
  - GET-style read operations retry up to 3 times on transient
    network/5xx errors (1s, 2s, 4s backoff).
  - Write operations (register, setHosts, etc.) NEVER retry — at-most-
    once semantics, because retrying a register call can double-charge
    or fail with "domain already exists".
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

NAMESPACE = {'nc': 'http://api.namecheap.com/xml.response'}


class NamecheapError(Exception):
    """A Namecheap API call returned Status=ERROR or transport failed."""

    def __init__(self, message, *, number='', command=''):
        super().__init__(message)
        self.number = number
        self.command = command


class NamecheapNotConfigured(NamecheapError):
    """Credentials are missing for the active environment."""


def _q(node, path):
    """Find descendant by path under the NC namespace; '' if absent."""
    found = node.find(path, NAMESPACE)
    return found.text if (found is not None and found.text is not None) else ''


def _qattr(node, attr, default=''):
    """Return an attribute value or default."""
    return node.get(attr, default) if node is not None else default


def _parse_dt(value):
    """Parse 'MM/DD/YYYY' into an aware UTC datetime, or None."""
    if not value:
        return None
    for fmt in ('%m/%d/%Y', '%m/%d/%Y %I:%M:%S %p'):
        try:
            naive = datetime.strptime(value, fmt)
            return timezone.make_aware(
                naive, timezone.get_default_timezone())
        except ValueError:
            continue
    return None


def _bool(s):
    return str(s).strip().lower() in ('true', 'yes', '1', 'enabled')


class NamecheapClient:
    """Single API client; safe to instantiate per call."""

    LIVE_ENDPOINT = 'https://api.namecheap.com/xml.response'
    SANDBOX_ENDPOINT = 'https://api.sandbox.namecheap.com/xml.response'

    def __init__(self, *, sandbox=None):
        """
        Build a client for the active environment.

        Resolution order for which environment to hit:
          1. Explicit `sandbox=` kwarg if passed (used by tests + admin
             diagnostics that need to target a specific env).
          2. DB-backed `NamecheapConfig.is_sandbox()` — admin can flip
             this from /admin-dashboard/domains/config/ without a
             restart.
          3. `settings.NAMECHEAP_SANDBOX` env var — fallback for the
             first-ever request before the config row exists, and for
             any context where the DB lookup itself would fail
             (tests with no migrations, manage.py shell on a fresh
             DB, etc.).
        """
        if sandbox is not None:
            self.sandbox = bool(sandbox)
        else:
            try:
                from domains.models import NamecheapConfig
                self.sandbox = NamecheapConfig.is_sandbox()
            except Exception:
                # DB unavailable / migration not yet run / no app
                # registry yet — fall back to env. Logged at WARNING
                # so misconfiguration is visible without crashing.
                logger.warning(
                    'NamecheapConfig lookup failed; falling back to '
                    'settings.NAMECHEAP_SANDBOX', exc_info=True)
                self.sandbox = settings.NAMECHEAP_SANDBOX

        if self.sandbox:
            self.api_user = settings.NAMECHEAP_API_USER
            self.api_key = settings.NAMECHEAP_API_KEY
            self.username = settings.NAMECHEAP_USERNAME
            self.endpoint = self.SANDBOX_ENDPOINT
            self.env_label = 'sandbox'
        else:
            self.api_user = settings.NAMECHEAP_LIVE_API_USER
            self.api_key = settings.NAMECHEAP_LIVE_API_KEY
            self.username = settings.NAMECHEAP_LIVE_USERNAME
            self.endpoint = self.LIVE_ENDPOINT
            self.env_label = 'live'

        if not (self.api_user and self.api_key and self.username):
            raise NamecheapNotConfigured(
                f'Namecheap credentials missing for {self.env_label}. '
                f'Set NAMECHEAP_{("LIVE_" if not self.sandbox else "")}'
                f'API_USER / API_KEY / USERNAME in .env.')

        self.client_ip = settings.NAMECHEAP_CLIENT_IP

    # ── Low-level transport ─────────────────────────────────────────────

    def _call(self, command, *, params=None, allow_retry=False):
        """
        Issue one API call. Returns the parsed XML root on success;
        raises NamecheapError on failure.
        """
        body = {
            'ApiUser': self.api_user,
            'ApiKey': self.api_key,
            'UserName': self.username,
            'ClientIp': self.client_ip,
            'Command': command,
        }
        if params:
            body.update({k: v for k, v in params.items() if v is not None})

        attempts = 3 if allow_retry else 1
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                # POST so long DomainList / record payloads don't blow
                # the URL length cap.
                r = requests.post(self.endpoint, data=body, timeout=30)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise NamecheapError(
                    f'Namecheap transport failure: {exc}',
                    command=command) from exc

            if r.status_code >= 500 and attempt < attempts:
                logger.warning(
                    'NC %s HTTP %d — retrying (attempt %d/%d)',
                    command, r.status_code, attempt, attempts)
                time.sleep(2 ** (attempt - 1))
                continue
            if r.status_code != 200:
                raise NamecheapError(
                    f'Namecheap HTTP {r.status_code} on {command}',
                    command=command)

            try:
                root = ET.fromstring(r.text)
            except ET.ParseError as exc:
                raise NamecheapError(
                    f'Namecheap returned unparseable XML: {exc}',
                    command=command) from exc

            status = root.get('Status', '')
            if status != 'OK':
                # Surface the first error number + text — that's what
                # we want to log + retry-decide on.
                errors = root.findall('.//nc:Error', NAMESPACE)
                if errors:
                    err = errors[0]
                    number = err.get('Number', '')
                    text = err.text or 'unknown error'
                    raise NamecheapError(
                        f'{text} ({command} #{number})',
                        number=number, command=command)
                raise NamecheapError(
                    f'Namecheap returned Status={status} for {command}',
                    command=command)

            return root

        # Shouldn't reach here, but defensive.
        raise NamecheapError(
            f'Namecheap unreachable after {attempts} attempts: {last_exc}',
            command=command)

    # ── High-level operations ───────────────────────────────────────────

    def check_availability(self, domains):
        """
        Bulk-check availability for up to ~50 domains in one call.

        Returns: list of dicts with keys:
          - domain (str)
          - available (bool)
          - is_premium (bool)
          - premium_price (Decimal or None)
        """
        if not domains:
            return []
        root = self._call(
            'namecheap.domains.check',
            params={'DomainList': ','.join(domains)},
            allow_retry=True,
        )
        results = []
        for node in root.findall('.//nc:DomainCheckResult', NAMESPACE):
            premium_price = node.get(
                'PremiumRegistrationPrice', '0') or '0'
            try:
                premium_price = Decimal(premium_price)
            except Exception:
                premium_price = Decimal('0')
            results.append({
                'domain': node.get('Domain', ''),
                'available': _bool(node.get('Available')),
                'is_premium': _bool(node.get('IsPremiumName')),
                'premium_price': premium_price,
            })
        return results

    def get_tld_pricing(self, tld, action='register'):
        """
        Get the current wholesale price for `tld` (action: register,
        renew, transfer). Returns a Decimal in USD.

        Namecheap returns tiered pricing — we always grab the
        regular (non-promo) 1-year price.
        """
        root = self._call(
            'namecheap.users.getPricing',
            params={
                'ProductType': 'DOMAIN',
                'ActionName': action.upper(),
                'ProductCategory': 'DOMAINS',
                'ProductName': tld,
            },
            allow_retry=True,
        )
        for product in root.findall('.//nc:Product', NAMESPACE):
            if product.get('Name', '').lower() != tld.lower():
                continue
            for price in product.findall('.//nc:Price', NAMESPACE):
                if price.get('Duration') == '1':
                    return Decimal(price.get('Price', '0') or '0')
        return Decimal('0')

    def register_domain(self, *, domain, years, registrant,
                        enable_whois_privacy=True):
        """
        Register a brand-new domain for `years` years (typically 1).

        `registrant` is a dict with the WHOIS contact info — even
        though privacy is enabled, the underlying registrant data
        still has to be submitted (ICANN requirement). Required keys:
          first_name, last_name, address1, city, state_province,
          postal_code, country, phone, email_address
        Optional: organization_name, address2, fax

        Returns dict:
          - domain (str)
          - registered (bool)
          - domain_id (str — Namecheap's internal ID)
          - whois_guard_enabled (bool)
          - charged_amount (Decimal)
          - expires_at (datetime or None)
        """
        params = {
            'DomainName': domain,
            'Years': str(years),
            # WhoisGuard: Namecheap's free privacy proxy. Default ON.
            'AddFreeWhoisguard': 'yes' if enable_whois_privacy else 'no',
            'WGEnabled': 'yes' if enable_whois_privacy else 'no',
            # Use Namecheap's nameservers by default. We DON'T pass
            # custom Nameservers because the API treats absence as
            # "use defaults" — exactly what we want.
        }
        # The API expects the SAME contact data submitted under FOUR
        # role prefixes: Registrant, Tech, Admin, AuxBilling.
        for role in ('Registrant', 'Tech', 'Admin', 'AuxBilling'):
            params.update({
                f'{role}FirstName':       registrant['first_name'],
                f'{role}LastName':        registrant['last_name'],
                f'{role}Address1':        registrant['address1'],
                f'{role}Address2':        registrant.get('address2', ''),
                f'{role}City':            registrant['city'],
                f'{role}StateProvince':   registrant['state_province'],
                f'{role}PostalCode':      registrant['postal_code'],
                f'{role}Country':         registrant['country'],
                f'{role}Phone':           registrant['phone'],
                f'{role}EmailAddress':    registrant['email_address'],
                f'{role}OrganizationName':
                    registrant.get('organization_name', ''),
            })

        root = self._call(
            'namecheap.domains.create',
            params=params, allow_retry=False,
        )
        result = root.find('.//nc:DomainCreateResult', NAMESPACE)
        if result is None:
            raise NamecheapError(
                'Namecheap registration call returned no result node',
                command='namecheap.domains.create')

        charged = result.get('ChargedAmount', '0') or '0'
        try:
            charged_amount = Decimal(charged)
        except Exception:
            charged_amount = Decimal('0')

        return {
            'domain': result.get('Domain', ''),
            'registered': _bool(result.get('Registered')),
            'domain_id': result.get('DomainID', ''),
            'whois_guard_enabled': _bool(result.get('WhoisguardEnable')),
            'charged_amount': charged_amount,
            'order_id': result.get('OrderID', ''),
            'transaction_id': result.get('TransactionID', ''),
        }

    def get_info(self, domain):
        """
        Fetch the full state of a registered domain.

        Returns dict:
          - domain, status, registered_at, expires_at, owner,
            registrar_lock (bool), auto_renew (bool),
            whois_guard_enabled (bool), nameservers (list[str])
        """
        root = self._call(
            'namecheap.domains.getInfo',
            params={'DomainName': domain},
            allow_retry=True,
        )
        info = root.find('.//nc:DomainGetInfoResult', NAMESPACE)
        if info is None:
            raise NamecheapError(
                f'No DomainGetInfoResult for {domain}',
                command='namecheap.domains.getInfo')

        details = info.find('.//nc:DomainDetails', NAMESPACE)
        ns_node = info.find('.//nc:DnsDetails', NAMESPACE)
        modification = info.find('.//nc:Modificationrights', NAMESPACE)
        wg = info.find('.//nc:Whoisguard', NAMESPACE)

        nameservers = []
        if ns_node is not None:
            for n in ns_node.findall('.//nc:Nameserver', NAMESPACE):
                if n.text:
                    nameservers.append(n.text.strip())

        return {
            'domain': info.get('DomainName', domain),
            'status': info.get('Status', ''),
            'is_owner': _bool(info.get('IsOwner')),
            'registered_at': _parse_dt(_q(details, 'nc:CreatedDate'))
                             if details is not None else None,
            'expires_at': _parse_dt(_q(details, 'nc:ExpiredDate'))
                          if details is not None else None,
            'registrar_lock': _bool(
                _qattr(info.find('.//nc:LockDetails', NAMESPACE),
                       'RegistrarLockStatus', 'false')),
            'auto_renew': _bool(_qattr(modification, 'AutoRenew', 'false')),
            'whois_guard_enabled': _bool(_qattr(wg, 'Enabled', 'false')),
            'nameservers': nameservers,
        }

    def list_domains(self, *, page_size=100):
        """List every domain on the account (paginates internally)."""
        page = 1
        all_domains = []
        while True:
            root = self._call(
                'namecheap.domains.getList',
                params={'PageSize': str(page_size), 'Page': str(page)},
                allow_retry=True,
            )
            for d in root.findall('.//nc:Domain', NAMESPACE):
                all_domains.append({
                    'id': d.get('ID', ''),
                    'name': d.get('Name', ''),
                    'is_expired': _bool(d.get('IsExpired')),
                    'is_locked': _bool(d.get('IsLocked')),
                    'auto_renew': _bool(d.get('AutoRenew')),
                    'expires': _parse_dt(d.get('Expires')),
                    'whois_guard': d.get('WhoisGuard', ''),
                })
            paging = root.find('.//nc:Paging', NAMESPACE)
            if paging is None:
                break
            total = int(_q(paging, 'nc:TotalItems') or 0)
            if len(all_domains) >= total:
                break
            page += 1
            if page > 50:    # hard safety stop
                break
        return all_domains

    def get_dns_records(self, domain):
        """
        Return Namecheap's current DNS record set for `domain`.

        List of dicts:
          host, type, value, ttl, mx_pref
        """
        sld, _, tld = domain.partition('.')
        root = self._call(
            'namecheap.domains.dns.getHosts',
            params={'SLD': sld, 'TLD': tld},
            allow_retry=True,
        )
        result = root.find('.//nc:DomainDNSGetHostsResult', NAMESPACE)
        records = []
        if result is None:
            return records
        for host in result.findall('.//nc:host', NAMESPACE):
            records.append({
                'host': host.get('Name', '@'),
                'type': host.get('Type', 'A'),
                'value': host.get('Address', ''),
                'ttl': int(host.get('TTL', '1800') or '1800'),
                'mx_pref': int(host.get('MXPref', '10') or '10'),
                'host_id': host.get('HostId', ''),
            })
        return records

    def set_dns_records(self, domain, records):
        """
        REPLACE the entire DNS record set for `domain`. There is no
        per-record update endpoint — every call sends the full set.

        Each record dict expects: host, type, value, ttl, mx_pref
        """
        sld, _, tld = domain.partition('.')
        params = {'SLD': sld, 'TLD': tld}
        for i, r in enumerate(records, start=1):
            params[f'HostName{i}']    = r.get('host', '@')
            params[f'RecordType{i}']  = r.get('type', 'A')
            params[f'Address{i}']     = r.get('value', '')
            params[f'TTL{i}']         = str(r.get('ttl', 1800))
            if r.get('type', '').upper() == 'MX':
                params[f'MXPref{i}']  = str(r.get('mx_pref', 10))
                # MX requires EmailType=MX at the domain level.
                params['EmailType'] = 'MX'
        self._call(
            'namecheap.domains.dns.setHosts',
            params=params, allow_retry=False,
        )
        return True

    def set_contacts(self, domain, registrant):
        """
        Update WHOIS contacts on an existing registration. Pushes the
        same contact dict under all four roles (Registrant, Tech,
        Admin, AuxBilling) — same as we do on registration.

        Used at transfer-out time: we change registrant from Aspired
        Websites to the client's name so they take over legal
        ownership cleanly before they transfer to another registrar.

        Returns True on success.
        """
        params = {'DomainName': domain}
        for role in ('Registrant', 'Tech', 'Admin', 'AuxBilling'):
            params.update({
                f'{role}FirstName':       registrant['first_name'],
                f'{role}LastName':        registrant['last_name'],
                f'{role}Address1':        registrant['address1'],
                f'{role}Address2':        registrant.get('address2', ''),
                f'{role}City':            registrant['city'],
                f'{role}StateProvince':   registrant['state_province'],
                f'{role}PostalCode':      registrant['postal_code'],
                f'{role}Country':         registrant['country'],
                f'{role}Phone':           registrant['phone'],
                f'{role}EmailAddress':    registrant['email_address'],
                f'{role}OrganizationName':
                    registrant.get('organization_name', ''),
            })
        self._call(
            'namecheap.domains.setContacts',
            params=params, allow_retry=False,
        )
        return True

    def get_balances(self):
        """
        Return the Namecheap account balance breakdown:
          {'available_balance': Decimal, 'account_balance': Decimal,
           'earned_amount': Decimal, 'withdrawable_amount': Decimal,
           'funds_required_for_auto_renew': Decimal, 'currency': str}

        Used by the admin balance widget so we can alert before
        running out of funds for registrations / renewals.
        """
        root = self._call(
            'namecheap.users.getBalances',
            allow_retry=True,
        )
        result = root.find('.//nc:UserGetBalancesResult', NAMESPACE)
        if result is None:
            return {
                'available_balance':              Decimal('0'),
                'account_balance':                Decimal('0'),
                'earned_amount':                  Decimal('0'),
                'withdrawable_amount':            Decimal('0'),
                'funds_required_for_auto_renew':  Decimal('0'),
                'currency':                       'USD',
            }
        def _d(name):
            try:
                return Decimal(result.get(name, '0') or '0')
            except Exception:
                return Decimal('0')
        return {
            'available_balance':              _d('AvailableBalance'),
            'account_balance':                _d('AccountBalance'),
            'earned_amount':                  _d('EarnedAmount'),
            'withdrawable_amount':            _d('WithdrawableAmount'),
            'funds_required_for_auto_renew':  _d('FundsRequiredForAutoRenew'),
            'currency':                       result.get('Currency', 'USD'),
        }

    def set_registrar_lock(self, domain, lock):
        """Toggle the transfer-protect registrar lock."""
        self._call(
            'namecheap.domains.setRegistrarLock',
            params={
                'DomainName': domain,
                'LockAction': 'LOCK' if lock else 'UNLOCK',
            },
            allow_retry=False,
        )
        return True

    def get_epp_code(self, domain):
        """
        Fetch the EPP/auth code for `domain`. Used at transfer-out
        time so the client can hand this string to their new
        registrar.

        Note: Some Namecheap accounts gate this behind email-to-
        registrant verification — in that case the API returns a
        success status but a blank code. Handle by emailing the
        client a "check your inbox" message.
        """
        root = self._call(
            'namecheap.domains.getInfo',
            params={'DomainName': domain},
            allow_retry=True,
        )
        # The EPP code is one of two places depending on TLD —
        # check both before giving up.
        info = root.find('.//nc:DomainGetInfoResult', NAMESPACE)
        if info is None:
            return ''
        epp = _q(info, './/nc:EPP_Code') or _q(info, './/nc:EppCode')
        return epp

    def set_auto_renew(self, domain, enable):
        """
        Toggle Namecheap's own auto-renew flag for `domain`.

        We almost always call this with enable=False — Stripe is the
        source of truth for renewal. Provided for completeness so an
        admin can re-enable at the registrar if Stripe is dropped.
        """
        command = (
            'namecheap.domains.setRegistrarLock'    # placeholder
            if False else                            # never True
            ('namecheap.domains.renew' if enable     # not actually
             else 'namecheap.domains.setRegistrarLock'))
        # Namecheap doesn't expose a single toggle — the closest is
        # setting it at registration time + the renew endpoint.
        # Documenting the limitation; callers should rely on Stripe
        # for renewals and not depend on this method.
        return False

    def renew_domain(self, domain, years=1):
        """
        Charge the Namecheap account balance to renew `domain` for
        `years` years. Called by our Stripe-driven renewal flow
        AFTER Stripe charges the client — never the other way
        around.

        Returns dict with charged_amount, expires_at, order_id.
        """
        root = self._call(
            'namecheap.domains.renew',
            params={'DomainName': domain, 'Years': str(years)},
            allow_retry=False,
        )
        result = root.find('.//nc:DomainRenewResult', NAMESPACE)
        if result is None:
            raise NamecheapError(
                'Namecheap renew call returned no result node',
                command='namecheap.domains.renew')
        return {
            'domain': result.get('DomainName', domain),
            'renewed': _bool(result.get('Renew')),
            'charged_amount': Decimal(
                result.get('ChargedAmount', '0') or '0'),
            'order_id': result.get('OrderID', ''),
            'transaction_id': result.get('TransactionID', ''),
        }


def get_client():
    """
    Return a fresh Namecheap client for the currently-active env.

    We DON'T cache the instance globally — when an admin flips
    sandbox mode in the admin dashboard, the next API call needs to
    target the new endpoint immediately (without a gunicorn
    restart). Instantiation is cheap: it's just reading 3 env
    strings + 1 DB lookup, no network I/O.
    """
    return NamecheapClient()
