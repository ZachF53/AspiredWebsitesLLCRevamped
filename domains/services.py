"""
Domain orchestration services.

Higher-level functions that combine the Namecheap API client + Stripe
helpers + local models. Views and Celery tasks should call into here
rather than the raw API client so the failure semantics stay
consistent.

Key invariants:
  - Stripe charge happens BEFORE Namecheap registration so a failed
    registration is cleanly refundable.
  - On Namecheap failure we IMMEDIATELY cancel + refund the Stripe
    sub so the client isn't out money.
  - Auto-A record is best-effort; failures don't block the rest of
    the flow.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from .models import DNSRecord, DomainRegistration, tier_slug_for_tld
from .namecheap_client import NamecheapError, get_client

logger = logging.getLogger(__name__)


# ── ASPIRED WEBSITES as the WHOIS registrant ──────────────────────────────
# Per the agency / managed-domain model (Option B), Aspired Websites is the
# public-facing registrant on every domain we register on a client's
# behalf. This means:
#   - Client never receives Namecheap / ICANN registration emails
#   - ICANN contact-verification is one-time for us (when we go live);
#     every subsequent registration reuses the verified contact
#   - All renewal / expiry / transfer notifications come to us
#   - On transfer-out we update the registrant to the CLIENT first
#     (via NamecheapClient.set_contacts) so they take ownership cleanly
#
# Constants kept here (not settings.py) because they are stable business-
# identity values, not deployment configuration.
ASPIRED_REGISTRANT = {
    'first_name': 'Zachery',
    'last_name': 'Long',
    'organization_name': 'Aspired Websites LLC',
    'address1': '8735 Dunwoody Place, Ste R',
    'address2': '',
    'city': 'Atlanta',
    'state_province': 'GA',
    'postal_code': '30350',
    'country': 'US',
    'phone': '+1.2108962536',
    'email_address': 'zachery@aspiredwebsites.com',
}


def aspired_registrant():
    """Return a fresh dict of the Aspired WHOIS registrant info."""
    return dict(ASPIRED_REGISTRANT)


# ── Public availability check ────────────────────────────────────────────────

def check_availability_all_tlds(name, tlds=None):
    """
    Check `name` across every TLD we offer (or the given subset).
    Returns a list of dicts, each with:
      {tld, domain, available, retail_price (Decimal), is_premium}
    """
    from decimal import Decimal
    from billing.pricing_models import ServiceTier

    tlds = tlds or ['com', 'net', 'org', 'law', 'legal', 'attorney']
    candidates = [f'{name}.{t}' for t in tlds]
    client = get_client()
    raw = client.check_availability(candidates)

    # Pre-load retail prices once, keyed by tier slug so adding new
    # premium TLDs is a one-line change in PREMIUM_TLDS (no second
    # edit needed here).
    standard = ServiceTier.objects.filter(slug='domain-standard').first()
    law = ServiceTier.objects.filter(slug='domain-law').first()
    price_by_slug = {
        'domain-standard': standard.price if standard else Decimal('75'),
        'domain-law':      law.price      if law      else Decimal('175'),
    }

    by_domain = {r['domain'].lower(): r for r in raw}
    out = []
    for tld in tlds:
        d = f'{name}.{tld}'
        r = by_domain.get(d.lower(), {})
        out.append({
            'tld': tld,
            'domain': d,
            'available': bool(r.get('available')),
            'retail_price': price_by_slug[tier_slug_for_tld(tld)],
            'is_premium': bool(r.get('is_premium')),
        })
    return out


# ── Registrant info ──────────────────────────────────────────────────────────

def registrant_from_client(client):
    """
    Build the WHOIS registrant dict from a ClientProfile.

    ICANN requires real contact data even when WHOIS privacy is on
    (the privacy proxy only masks the public-facing record). If any
    required field is missing on the profile, raises ValueError —
    caller should redirect to the settings page.
    """
    contact = (client.contact_name or client.firm_name or '').strip()
    parts = contact.split(' ', 1)
    first = parts[0] if parts else ''
    last = parts[1] if len(parts) > 1 else parts[0]

    missing = []
    if not first or not last:
        missing.append('contact name (first + last)')
    if not client.address:
        missing.append('street address')
    if not client.city:
        missing.append('city')
    if not client.state:
        missing.append('state')
    if not client.zip_code:
        missing.append('ZIP code')
    if not client.phone:
        missing.append('phone')
    if not client.user or not client.user.email:
        missing.append('email')
    if missing:
        raise ValueError(
            'Your account is missing the following info required for '
            'domain registration: ' + ', '.join(missing) +
            '. Please update your profile on the Settings page.')

    # Namecheap phone format: +CC.NNNNNNNNNN — assume US (+1) if not
    # already a +country-prefixed format.
    raw_phone = ''.join(c for c in client.phone if c.isdigit())
    if len(raw_phone) == 10:
        phone = f'+1.{raw_phone}'
    elif len(raw_phone) == 11 and raw_phone.startswith('1'):
        phone = f'+1.{raw_phone[1:]}'
    else:
        phone = f'+1.{raw_phone}'

    return {
        'first_name': first,
        'last_name': last,
        'organization_name': client.firm_name or '',
        'address1': client.address,
        'city': client.city,
        'state_province': client.state,
        'postal_code': client.zip_code,
        'country': 'US',
        'phone': phone,
        'email_address': client.user.email,
    }


# ── Registration ─────────────────────────────────────────────────────────────

def register_domain_for_client(client, domain_name, tld):
    """
    End-to-end domain registration. Atomic-ish via Stripe-first then
    Namecheap-with-refund-on-failure.

    Steps:
      1. Validate inputs (tld supported, name shape, etc.)
      2. Build registrant info (raises ValueError if profile incomplete)
      3. Re-check availability against Namecheap (the search result may
         be stale by minutes; someone else may have grabbed it)
      4. Create the DomainRegistration row (status=pending)
      5. Create the Stripe subscription — charges the customer's
         default card immediately
      6. Call Namecheap to register
      7. On Namecheap failure: refund + cancel sub, mark registration
         status=failed, raise
      8. On success: set status=active, registered/expires timestamps,
         auto-A record if client has a Droplet

    Returns the DomainRegistration row.

    SANDBOX MODE BYPASS:
    When NamecheapConfig.is_sandbox() is True, this function does NOT
    create a Stripe subscription and does NOT require a payment
    method — the whole flow runs free against the Namecheap sandbox
    so an admin (or curious client) can rehearse the UI without
    burning real money. The DomainRegistration row gets
    stripe_subscription_id='' as the marker. Flipping back to live
    mode means future registrations charge normally — old sandbox
    registrations are just informational artifacts.

    Raises:
      ValueError                — bad inputs, profile incomplete, name
                                  no longer available
      NamecheapError            — registration failed (Stripe refunded)
      Stripe / Django errors    — propagated; caller logs + flashes
    """
    from billing.stripe_helpers import (
        create_domain_subscription,
        refund_failed_domain_registration,
    )
    from domains.models import NamecheapConfig

    is_sandbox = NamecheapConfig.is_sandbox()

    tld = tld.lower().lstrip('.')
    domain_name = domain_name.lower()
    if not domain_name.endswith(f'.{tld}'):
        domain_name = f'{domain_name}.{tld}'

    # Bare-min syntactic check — Namecheap will reject anything else.
    sld = domain_name[:-(len(tld) + 1)]
    if len(sld) < 1 or len(sld) > 63:
        raise ValueError(f'Domain name "{sld}" must be 1-63 chars.')
    if not all(c.isalnum() or c == '-' for c in sld):
        raise ValueError(
            f'Domain name "{sld}" can only contain letters, digits, '
            f'and hyphens.')
    if sld.startswith('-') or sld.endswith('-'):
        raise ValueError(f'Domain name "{sld}" cannot start or end with -.')

    # 1. Re-check availability.
    nc = get_client()
    avail = nc.check_availability([domain_name])
    if not avail or not avail[0].get('available'):
        raise ValueError(
            f'{domain_name} is no longer available. Search again for '
            f'an alternate name.')
    if avail[0].get('is_premium'):
        # We don't sell premium-priced names through self-serve flow.
        raise ValueError(
            f'{domain_name} is a premium name. Contact us directly '
            f'to register it.')

    # 2. Build registrant — always Aspired Websites (Option B). The
    # client never receives Namecheap emails this way. Client owns
    # the domain in our DB and via the contract; legal ownership
    # transfers to them via set_contacts during transfer-out.
    registrant = aspired_registrant()

    # 3. Create the local row in pending state.
    registration = DomainRegistration.objects.create(
        client=client,
        domain_name=domain_name,
        tld=tld,
        status='pending',
        whois_privacy_enabled=True,
        registrar_lock=True,
        pricing_tier_slug=tier_slug_for_tld(tld),
    )

    # 4. Charge via Stripe — UNLESS we're in sandbox mode, in which
    # case skip Stripe entirely. Sandbox is for free rehearsal of
    # the whole flow; charging a real card while pretending the
    # domain is real would be the exact opposite of that.
    sub_id_for_refund = ''
    if is_sandbox:
        registration.internal_notes = (
            (registration.internal_notes or '')
            + '\n[Sandbox registration — no Stripe sub created]'
        ).strip()
        registration.save(update_fields=[
            'internal_notes', 'updated_at'])
    else:
        try:
            sub = create_domain_subscription(client, registration)
            sub_id_for_refund = getattr(sub, 'id', '') or ''
        except Exception as exc:
            registration.status = 'failed'
            registration.last_api_error = f'Stripe: {exc}'[:2000]
            registration.save(update_fields=[
                'status', 'last_api_error', 'updated_at'])
            raise

    # 5. Register with Namecheap.
    try:
        result = nc.register_domain(
            domain=domain_name,
            years=1,
            registrant=registrant,
            enable_whois_privacy=True,
        )
        if not result.get('registered'):
            raise NamecheapError(
                f'Namecheap reported domain not registered for {domain_name}',
                command='namecheap.domains.create')
    except NamecheapError as exc:
        logger.exception(
            'Namecheap registration failed for %s — refunding %s',
            domain_name, sub_id_for_refund)
        if sub_id_for_refund:
            refunded = refund_failed_domain_registration(
                sub_id_for_refund,
                reason=f'Namecheap registration failed: {exc}',
            )
        else:
            # Sandbox flow — no Stripe sub was ever created so
            # there's nothing to refund. Treat as refund-succeeded
            # so the user-facing message stays clean.
            refunded = True
        registration.status = 'failed'
        registration.last_api_error = f'Namecheap: {exc}'[:2000]
        registration.save(update_fields=[
            'status', 'last_api_error', 'updated_at'])
        # Re-raise with a flag indicating whether refund cleared so
        # the caller's flash message can be accurate.
        exc.refund_succeeded = refunded
        raise

    # 6. Mark active.
    registration.status = 'active'
    registration.registered_at = timezone.now()
    registration.expires_at = timezone.now() + timedelta(days=365)
    registration.last_synced_at = timezone.now()
    registration.last_api_call_at = timezone.now()
    registration.last_api_error = ''
    registration.save(update_fields=[
        'status', 'registered_at', 'expires_at', 'last_synced_at',
        'last_api_call_at', 'last_api_error', 'updated_at',
    ])
    logger.info(
        'register_domain_for_client: %s registered for client %s',
        domain_name, client.pk)

    # 7. Auto-A record — best effort, do NOT raise.
    if client.do_droplet_ip:
        try:
            set_auto_a_record(registration, str(client.do_droplet_ip))
        except Exception:
            logger.exception(
                'Auto-A record failed for new registration %s',
                registration.pk)

    # 8. Branded confirmation email — best effort.
    try:
        from domains.emails import send_registered_email
        send_registered_email(registration)
    except Exception:
        logger.exception(
            'Registered email failed for %s', registration.domain_name)

    return registration


def admin_register_domain_for_client(
        client, domain_name, tld, *, send_email=True,
        internal_notes=''):
    """
    Admin-side registration. Same as `register_domain_for_client`
    EXCEPT:
      - No Stripe Subscription is created — admin registrations are
        gift / promo / migration cases that the business swallows
        the Namecheap cost on
      - The Namecheap account balance still gets debited; that's
        intentional and visible from the NC dashboard
      - No 'is_premium' rejection — admin can override and register
        premium names manually (warns but proceeds)
      - Notes field is exposed so the admin can record WHY it was
        gifted (referral / migration from another registrar / etc.)

    The DomainRegistration row has stripe_subscription_id='' as the
    marker that this isn't billed. The renewal-gate webhook handler
    treats no-sub domains as "let it expire" — admin can re-register
    or set up a Stripe sub later.

    Returns the DomainRegistration row.

    Raises:
      ValueError      — bad inputs, name unavailable, profile
                        missing required WHOIS fields
      NamecheapError  — registration failed at NC
    """
    tld = tld.lower().lstrip('.')
    domain_name = domain_name.lower()
    if not domain_name.endswith(f'.{tld}'):
        domain_name = f'{domain_name}.{tld}'

    sld = domain_name[:-(len(tld) + 1)]
    if len(sld) < 1 or len(sld) > 63:
        raise ValueError(f'Domain name "{sld}" must be 1-63 chars.')
    if not all(c.isalnum() or c == '-' for c in sld):
        raise ValueError(
            f'Domain name "{sld}" can only contain letters, digits, '
            f'and hyphens.')
    if sld.startswith('-') or sld.endswith('-'):
        raise ValueError(
            f'Domain name "{sld}" cannot start or end with -.')

    nc = get_client()
    avail = nc.check_availability([domain_name])
    if not avail or not avail[0].get('available'):
        raise ValueError(
            f'{domain_name} is not available on the current '
            f'Namecheap environment.')

    # NOTE: no premium rejection — admin can override.

    # WHOIS registrant: Aspired Websites (Option B managed model).
    # Same reasoning as the client-facing path.
    registrant = aspired_registrant()

    registration = DomainRegistration.objects.create(
        client=client,
        domain_name=domain_name,
        tld=tld,
        status='pending',
        whois_privacy_enabled=True,
        registrar_lock=True,
        pricing_tier_slug=tier_slug_for_tld(tld),
        internal_notes=internal_notes,
    )

    try:
        result = nc.register_domain(
            domain=domain_name,
            years=1,
            registrant=registrant,
            enable_whois_privacy=True,
        )
        if not result.get('registered'):
            raise NamecheapError(
                f'Namecheap reported domain not registered for {domain_name}',
                command='namecheap.domains.create')
    except NamecheapError as exc:
        logger.exception(
            'Admin Namecheap registration failed for %s', domain_name)
        registration.status = 'failed'
        registration.last_api_error = f'Namecheap: {exc}'[:2000]
        registration.save(update_fields=[
            'status', 'last_api_error', 'updated_at'])
        raise

    registration.status = 'active'
    registration.registered_at = timezone.now()
    registration.expires_at = timezone.now() + timedelta(days=365)
    registration.last_synced_at = timezone.now()
    registration.last_api_call_at = timezone.now()
    registration.last_api_error = ''
    registration.save(update_fields=[
        'status', 'registered_at', 'expires_at', 'last_synced_at',
        'last_api_call_at', 'last_api_error', 'updated_at',
    ])
    logger.info(
        'admin_register_domain_for_client: %s registered for client '
        '%s (no Stripe sub — admin gift)', domain_name, client.pk)

    if client.do_droplet_ip:
        try:
            set_auto_a_record(
                registration, str(client.do_droplet_ip))
        except Exception:
            logger.exception(
                'Auto-A record failed for admin registration %s',
                registration.pk)

    if send_email:
        try:
            from domains.emails import send_registered_email
            send_registered_email(registration)
        except Exception:
            logger.exception(
                'Registered email failed for %s',
                registration.domain_name)

    return registration


# ── DNS management ───────────────────────────────────────────────────────────

def set_auto_a_record(registration, ip_address):
    """
    Replace the apex A record with one pointing at `ip_address`, plus
    a `www` CNAME to the apex. Marks both as auto_managed so the
    portal UI can distinguish them from client-edited records.

    Pulls the current record set, replaces (or inserts) the apex A +
    www CNAME, pushes the full set back to Namecheap, and mirrors the
    set locally.
    """
    nc = get_client()

    existing = list(registration.dns_records.all())
    # Drop any current apex-A or www-CNAME so we don't push duplicates.
    keep = [r for r in existing if not (
        (r.host in ('@', '') and r.record_type == 'A')
        or (r.host == 'www' and r.record_type in ('CNAME', 'A'))
    )]

    # Build the new record set: apex A + www CNAME + everything else.
    new_records = [
        {
            'host': '@',
            'type': 'A',
            'value': ip_address,
            'ttl': 1800,
            'auto_managed': True,
        },
        {
            'host': 'www',
            'type': 'CNAME',
            'value': registration.domain_name + '.',
            'ttl': 1800,
            'auto_managed': True,
        },
    ]
    for r in keep:
        new_records.append({
            'host': r.host,
            'type': r.record_type,
            'value': r.value,
            'ttl': r.ttl,
            'mx_pref': r.mx_priority,
            'auto_managed': r.auto_managed,
        })

    nc.set_dns_records(registration.domain_name, new_records)

    # Mirror the new set locally.
    DNSRecord.objects.filter(domain=registration).delete()
    for r in new_records:
        DNSRecord.objects.create(
            domain=registration,
            record_type=r['type'],
            host=r['host'],
            value=r['value'],
            ttl=r.get('ttl', 1800),
            mx_priority=r.get('mx_pref', 10),
            auto_managed=r.get('auto_managed', False),
        )

    registration.auto_a_record_set_at = timezone.now()
    registration.last_api_call_at = timezone.now()
    registration.save(update_fields=[
        'auto_a_record_set_at', 'last_api_call_at', 'updated_at'])


def replace_dns_records(registration, records):
    """
    Replace the entire DNS record set for `registration` (Namecheap
    has no per-record update endpoint — every change is a full
    re-push). Mirrors the records locally on success.

    `records` is a list of dicts with: host, type, value, ttl,
    mx_pref. Auto-managed records the caller hasn't included will be
    dropped — caller is responsible for re-including any auto-A row
    they want preserved.
    """
    nc = get_client()
    nc.set_dns_records(registration.domain_name, records)

    DNSRecord.objects.filter(domain=registration).delete()
    for r in records:
        DNSRecord.objects.create(
            domain=registration,
            record_type=r.get('type', 'A'),
            host=r.get('host', '@'),
            value=r.get('value', ''),
            ttl=r.get('ttl', 1800),
            mx_priority=r.get('mx_pref', 10),
            auto_managed=r.get('auto_managed', False),
        )
    registration.last_api_call_at = timezone.now()
    registration.save(update_fields=['last_api_call_at', 'updated_at'])


# ── Sync ─────────────────────────────────────────────────────────────────────

def sync_one(registration):
    """
    Pull current state from Namecheap for `registration` and mirror
    expiry / lock / privacy flags. Updates `last_synced_at` always
    (success or failure), `last_api_error` only on failure.
    """
    nc = get_client()
    fields = ['last_synced_at', 'updated_at']
    registration.last_synced_at = timezone.now()
    try:
        info = nc.get_info(registration.domain_name)
        registration.registered_at = (
            info.get('registered_at') or registration.registered_at)
        registration.expires_at = (
            info.get('expires_at') or registration.expires_at)
        registration.registrar_lock = info.get('registrar_lock', False)
        registration.auto_renew_at_registrar = info.get(
            'auto_renew', False)
        registration.whois_privacy_enabled = info.get(
            'whois_guard_enabled', registration.whois_privacy_enabled)
        registration.nameservers = info.get('nameservers', []) or []
        registration.last_api_error = ''
        fields.extend([
            'registered_at', 'expires_at', 'registrar_lock',
            'auto_renew_at_registrar', 'whois_privacy_enabled',
            'nameservers', 'last_api_error',
        ])
    except NamecheapError as exc:
        registration.last_api_error = str(exc)[:2000]
        fields.append('last_api_error')
    registration.save(update_fields=fields)


# ── Cancel / transfer-out ────────────────────────────────────────────────────

def begin_transfer_out(registration, reason=''):
    """
    Start the transfer-out / cancel flow for `registration`.

    Five steps in order (each best-effort, never fail the whole flow):
      1. Update WHOIS registrant from Aspired -> the client (so they
         take legal ownership cleanly before transfer). Skipped if
         the client's profile is incomplete; admin emailed instead.
      2. Lift the registrar lock so a transfer-out can be initiated
      3. Pull the EPP code from Namecheap
      4. Set local status='grace' + cancel the Stripe sub at period end
      5. Email the client the transfer-out package with EPP + steps

    Returns the EPP code (already saved encrypted on the row).
    """
    from billing.stripe_helpers import cancel_domain_subscription
    from domains.emails import send_transfer_out_email

    nc = get_client()
    epp = ''

    # 1. Update WHOIS to the client (Option B managed-model handover).
    # If their profile is incomplete we keep Aspired as registrant
    # for now — admin can manually update once the client fills in
    # their settings. Logged either way so an admin can review.
    try:
        client_registrant = registrant_from_client(registration.client)
        nc.set_contacts(registration.domain_name, client_registrant)
        logger.info(
            'begin_transfer_out: registrant updated to client for %s',
            registration.domain_name)
    except ValueError as exc:
        # Profile incomplete — proceed with Aspired as registrant.
        # Admin gets the alert at end so they can resolve manually.
        logger.warning(
            'begin_transfer_out: cannot update registrant for %s — '
            'client profile incomplete (%s). Aspired remains '
            'registrant; admin will need to update manually.',
            registration.domain_name, exc)
        registration.last_api_error = (
            f'registrant update skipped: {exc}'[:2000])
    except NamecheapError as exc:
        logger.exception(
            'set_contacts failed for %s', registration.domain_name)
        registration.last_api_error = f'set_contacts: {exc}'[:2000]

    # 2. Lift the registrar lock.
    try:
        nc.set_registrar_lock(registration.domain_name, lock=False)
        registration.registrar_lock = False
    except NamecheapError as exc:
        logger.exception(
            'Unlock failed for %s during transfer-out',
            registration.domain_name)
        registration.last_api_error = f'unlock: {exc}'[:2000]

    # 3. Pull EPP code.
    try:
        epp = nc.get_epp_code(registration.domain_name)
    except NamecheapError as exc:
        logger.exception(
            'EPP fetch failed for %s', registration.domain_name)
        registration.last_api_error = f'epp: {exc}'[:2000]
        epp = ''

    if epp:
        registration.set_epp_code(epp)
    registration.status = 'grace'
    registration.save()

    # 4. Cancel the Stripe sub at period end.
    try:
        cancel_domain_subscription(registration, reason=reason)
    except Exception:
        logger.exception(
            'Stripe sub cancel failed for %s', registration.domain_name)

    # 5. Email the client.
    try:
        send_transfer_out_email(registration, epp)
    except Exception:
        logger.exception(
            'Transfer-out email failed for %s', registration.domain_name)

    return epp


# ── Resume / undo cancel ────────────────────────────────────────────────────

def resume_domain(registration):
    """
    Undo a cancel-at-period-end on a domain registration. Inverse of
    `begin_transfer_out`:
      1. Re-lock the domain at Namecheap (registrar lock back on)
      2. Update WHOIS back to Aspired Websites (in case it was changed
         to the client during the previous transfer-out attempt)
      3. Cancel the Stripe-side cancel (cancel_at_period_end=False)
      4. Clear the locally-stored EPP code (it would have been
         compromised by the prior email; force them to re-cancel
         to get a fresh one)
      5. Status back to 'active'

    Returns the updated DomainRegistration. Raises ValueError if the
    registration isn't currently in grace status — there's nothing
    to resume from any other state.
    """
    from billing.stripe_helpers import resume_domain_subscription

    if registration.status != 'grace':
        raise ValueError(
            f'Cannot resume {registration.domain_name} — it is '
            f'{registration.get_status_display()}, not in grace.')

    nc = get_client()

    # 1. Re-lock at registrar.
    try:
        nc.set_registrar_lock(registration.domain_name, lock=True)
        registration.registrar_lock = True
    except NamecheapError as exc:
        logger.exception(
            'Re-lock failed for %s during resume', registration.domain_name)
        registration.last_api_error = f're-lock: {exc}'[:2000]

    # 2. Restore Aspired as registrant.
    try:
        nc.set_contacts(registration.domain_name, aspired_registrant())
    except NamecheapError as exc:
        logger.exception(
            'restore-registrant failed for %s', registration.domain_name)
        registration.last_api_error = (
            f'restore registrant: {exc}'[:2000])

    # 3. Cancel the Stripe cancel.
    try:
        resume_domain_subscription(registration)
    except Exception:
        logger.exception(
            'Stripe resume failed for %s', registration.domain_name)

    # 4. Clear EPP code — the one we already emailed is compromised.
    registration.set_epp_code('')

    # 5. Back to active.
    registration.status = 'active'
    registration.last_api_call_at = timezone.now()
    registration.save()
    logger.info(
        'resume_domain: %s back to active for client %s',
        registration.domain_name, registration.client_id)

    return registration


# ── Park (hosting cancelled, domain stays) ─────────────────────────────────

def park_domain(registration):
    """
    Replace DNS with URL301 redirects pointing at our parking page.

    Called when the client cancels hosting but keeps the domain.
    Instead of letting visitors hit a dead Droplet IP and see
    "connection refused", their browser gets a clean HTTP 301 to
    https://aspiredwebsites.com/parked/?for=<their-domain>.

    Implemented via Namecheap's URL301 record type so we don't
    need any nginx config or shared IP infrastructure for this.
    Apex + www both redirect.
    """
    nc = get_client()
    parking_url = (
        f'https://aspiredwebsites.com/parked/'
        f'?for={registration.domain_name}'
    )
    new_records = [
        {'host': '@',   'type': 'URL301',
         'value': parking_url, 'ttl': 1800, 'auto_managed': True},
        {'host': 'www', 'type': 'URL301',
         'value': parking_url, 'ttl': 1800, 'auto_managed': True},
    ]
    nc.set_dns_records(registration.domain_name, new_records)

    # Mirror locally.
    DNSRecord.objects.filter(domain=registration).delete()
    for r in new_records:
        DNSRecord.objects.create(
            domain=registration,
            record_type=r['type'],
            host=r['host'],
            value=r['value'],
            ttl=r['ttl'],
            auto_managed=True,
        )

    registration.parked_at = timezone.now()
    registration.auto_a_record_set_at = None
    registration.last_api_call_at = timezone.now()
    registration.save(update_fields=[
        'parked_at', 'auto_a_record_set_at',
        'last_api_call_at', 'updated_at',
    ])
    logger.info(
        'park_domain: %s now points at parking page',
        registration.domain_name)


def unpark_domain(registration, new_ip):
    """
    Inverse of park_domain — replace the URL301s with apex A + www
    CNAME pointing at `new_ip`. Called when a parked domain gets
    new hosting and needs to come back online.
    """
    set_auto_a_record(registration, new_ip)
    registration.parked_at = None
    registration.save(update_fields=['parked_at', 'updated_at'])
    logger.info(
        'unpark_domain: %s repointed to %s',
        registration.domain_name, new_ip)
