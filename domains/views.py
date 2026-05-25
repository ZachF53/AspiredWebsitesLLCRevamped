"""Client-portal domain views."""

import logging
import re

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.decorators import client_required

from .models import (
    DNS_RECORD_TYPE_CHOICES,
    DNSRecord,
    DomainRegistration,
    TLD_CHOICES,
)
from .namecheap_client import NamecheapError
from .services import (
    begin_transfer_out,
    check_availability_all_tlds,
    register_domain_for_client,
    replace_dns_records,
    sync_one,
)

logger = logging.getLogger(__name__)


# ── Shared helpers ──────────────────────────────────────────────────────────

# Reuse the portal sidebar context populated by clients.views._portal_context.
def _portal_ctx(request, active_nav, **extra):
    from clients.views import _portal_context
    return _portal_context(request, active_nav, **extra)


_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$', re.I)


def _normalize_name(raw):
    """Lower-case, strip any trailing .tld the user pasted in."""
    raw = (raw or '').strip().lower()
    # Allow user to paste a full domain — pull out the SLD.
    if '.' in raw:
        raw = raw.split('.', 1)[0]
    return raw


# ── /portal/domains/ — list ────────────────────────────────────────────────

@client_required
def portal_domains(request):
    """List every domain the client has registered through us."""
    profile = request.client_profile
    registrations = (
        DomainRegistration.objects
        .filter(client=profile)
        .order_by('-created_at')
    )
    ctx = _portal_ctx(
        request, 'domains',
        registrations=registrations,
        has_any_domain=registrations.exists(),
    )
    return render(request, 'domains/portal_domains.html', ctx)


# ── /portal/domains/search/ — find one ─────────────────────────────────────

@client_required
def portal_domains_search(request):
    """
    Domain search page. GET shows the form; POST (or GET with ?q=)
    runs the availability check across all 6 TLDs.
    """
    raw_query = request.POST.get('name') or request.GET.get('q') or ''
    sld = _normalize_name(raw_query)
    results = []
    error = ''

    if sld:
        if not _NAME_RE.match(sld):
            error = (
                'Domain names can only contain letters, digits, and '
                'hyphens, and must be 1-63 characters long.')
        else:
            try:
                results = check_availability_all_tlds(sld)
            except NamecheapError as exc:
                logger.exception('Namecheap availability check failed')
                error = (
                    'We couldn\'t reach the registrar to check '
                    'availability — please try again in a moment.')

    ctx = _portal_ctx(
        request, 'domains',
        query=sld,
        results=results,
        error=error,
        tld_count=len(TLD_CHOICES),
    )
    return render(request, 'domains/portal_domains_search.html', ctx)


# ── /portal/domains/register/<domain>/ — confirm + POST register ───────────

@client_required
def portal_domain_register(request, domain):
    """
    GET  — confirmation screen for a domain registration.
    POST — actually register: charges card via Stripe, registers via
           Namecheap, auto-points A record to client's Droplet.

    Routes go via `register_domain_for_client` which is responsible
    for the full Stripe-first-with-refund-on-failure flow.
    """
    from billing.stripe_helpers import (
        get_customer_default_payment_method,
        list_customer_payment_methods,
    )

    profile = request.client_profile

    domain = (domain or '').lower().strip()
    if '.' not in domain:
        messages.error(request, 'Invalid domain name.')
        return redirect('domains:portal_domains_search')
    sld, _, tld = domain.partition('.')
    if not _NAME_RE.match(sld) or tld not in dict(TLD_CHOICES):
        messages.error(request, 'Unsupported domain or TLD.')
        return redirect('domains:portal_domains_search')

    # Default card lookup for the confirm page.
    default_card = None
    if profile.stripe_customer_id:
        try:
            pm_id = get_customer_default_payment_method(
                profile.stripe_customer_id)
            if pm_id:
                methods = list_customer_payment_methods(
                    profile.stripe_customer_id)
                for m in methods:
                    if getattr(m, 'id', '') == pm_id:
                        default_card = {
                            'brand':     (getattr(m.card, 'brand', '') or '').upper(),
                            'last4':     getattr(m.card, 'last4', ''),
                            'exp_month': getattr(m.card, 'exp_month', ''),
                            'exp_year':  getattr(m.card, 'exp_year', ''),
                        }
                        break
        except Exception:
            logger.exception('Default-card lookup failed for %s', profile.pk)

    # Pre-resolve price for display.
    from billing.stripe_helpers import get_domain_tier
    try:
        tier = get_domain_tier(tld)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('domains:portal_domains_search')

    if request.method == 'POST':
        if not default_card:
            messages.error(
                request,
                'Add a payment method first — domain registration '
                'needs a card on file.')
            return redirect('clients:portal_subscriptions')

        try:
            registration = register_domain_for_client(profile, sld, tld)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('domains:portal_domains_search')
        except NamecheapError as exc:
            refunded = getattr(exc, 'refund_succeeded', False)
            if refunded:
                messages.error(
                    request,
                    f'We weren\'t able to register {domain} '
                    f'({exc}). Your card has been refunded.')
            else:
                messages.error(
                    request,
                    f'We weren\'t able to register {domain} '
                    f'({exc}) — and the refund didn\'t go through '
                    f'automatically. We\'ve been notified and will '
                    f'reach out shortly.')
            return redirect('domains:portal_domains_search')
        except Exception as exc:  # noqa: BLE001
            logger.exception('Domain registration failed')
            messages.error(
                request,
                f'Domain registration failed: {exc}')
            return redirect('domains:portal_domains_search')

        messages.success(
            request,
            f'{registration.domain_name} is registered!')
        return redirect('domains:portal_domain_detail', pk=registration.pk)

    ctx = _portal_ctx(
        request, 'domains',
        domain=domain,
        sld=sld,
        tld=tld,
        tier=tier,
        default_card=default_card,
        profile_complete=_is_profile_complete(profile),
    )
    return render(request, 'domains/portal_domain_register.html', ctx)


def _is_profile_complete(profile):
    """Has the profile got everything we need for WHOIS registrant?"""
    user_email = getattr(profile.user, 'email', '') if profile.user else ''
    return all([
        (profile.contact_name or '').strip(),
        profile.address, profile.city, profile.state, profile.zip_code,
        profile.phone, user_email,
    ])


# ── /portal/domains/<id>/ — detail ─────────────────────────────────────────

@client_required
def portal_domain_detail(request, pk):
    """Domain detail — status, nameservers, DNS records, transfer-out."""
    profile = request.client_profile
    registration = get_object_or_404(
        DomainRegistration, pk=pk, client=profile)

    # Refresh state from Namecheap if it's been more than an hour
    # since the last sync — keeps the page accurate without an
    # explicit refresh button click.
    needs_sync = (
        registration.status in ('active', 'grace')
        and (not registration.last_synced_at
             or (timezone.now() - registration.last_synced_at).total_seconds() > 3600)
    )
    if needs_sync:
        try:
            sync_one(registration)
        except Exception:
            logger.exception('Background sync failed for %s', registration.pk)

    records = registration.dns_records.all().order_by('host', 'record_type')
    epp_code = registration.decrypt_epp_code() if registration.status == 'grace' else ''

    ctx = _portal_ctx(
        request, 'domains',
        reg=registration,
        records=records,
        epp_code=epp_code,
    )
    return render(request, 'domains/portal_domain_detail.html', ctx)


# ── /portal/domains/<id>/dns/ — edit DNS records ───────────────────────────

@client_required
def portal_domain_dns(request, pk):
    """GET edit form; POST replaces full record set."""
    profile = request.client_profile
    registration = get_object_or_404(
        DomainRegistration, pk=pk, client=profile)

    if registration.status != 'active':
        messages.error(
            request,
            'DNS editing is only available on active domains.')
        return redirect('domains:portal_domain_detail', pk=pk)

    if request.method == 'POST':
        # Re-build the record set from POST. Submitted form has
        # parallel arrays: types[], hosts[], values[], ttls[],
        # mx_prefs[]. Drop any rows with an empty value.
        types = request.POST.getlist('types[]')
        hosts = request.POST.getlist('hosts[]')
        values = request.POST.getlist('values[]')
        ttls = request.POST.getlist('ttls[]')
        prefs = request.POST.getlist('mx_prefs[]')

        new_records = []
        valid_types = {k for k, _ in DNS_RECORD_TYPE_CHOICES}
        for i, raw_value in enumerate(values):
            value = (raw_value or '').strip()
            if not value:
                continue
            r_type = (types[i] if i < len(types) else 'A').upper()
            if r_type not in valid_types:
                continue
            host = (hosts[i] if i < len(hosts) else '@').strip() or '@'
            try:
                ttl = int(ttls[i] if i < len(ttls) else 1800)
            except (ValueError, TypeError):
                ttl = 1800
            ttl = max(60, min(ttl, 86400))
            try:
                mx_pref = int(prefs[i] if i < len(prefs) else 10)
            except (ValueError, TypeError):
                mx_pref = 10
            new_records.append({
                'host': host,
                'type': r_type,
                'value': value,
                'ttl': ttl,
                'mx_pref': mx_pref,
            })

        try:
            replace_dns_records(registration, new_records)
        except NamecheapError as exc:
            messages.error(
                request, f'Namecheap rejected the record set: {exc}')
            return redirect('domains:portal_domain_dns', pk=pk)
        except Exception as exc:  # noqa: BLE001
            logger.exception('DNS update failed for %s', registration.pk)
            messages.error(request, f'DNS update failed: {exc}')
            return redirect('domains:portal_domain_dns', pk=pk)

        messages.success(
            request,
            'DNS records saved. Changes propagate in 5-15 minutes.')
        return redirect('domains:portal_domain_detail', pk=pk)

    records = list(registration.dns_records.all().order_by('host', 'record_type'))
    ctx = _portal_ctx(
        request, 'domains',
        reg=registration,
        records=records,
        record_types=DNS_RECORD_TYPE_CHOICES,
    )
    return render(request, 'domains/portal_domain_dns.html', ctx)


# ── /portal/domains/<id>/cancel/ — transfer-out + cancel ───────────────────

@client_required
@require_POST
def portal_domain_cancel(request, pk):
    """
    Begin the cancel + transfer-out flow.

    Sets registration.status='grace', cancels Stripe sub at period
    end, lifts registrar lock, pulls EPP code, and emails the client
    the transfer-out package.
    """
    profile = request.client_profile
    registration = get_object_or_404(
        DomainRegistration, pk=pk, client=profile)
    if registration.status not in ('active',):
        messages.info(
            request,
            'This domain isn\'t in a state where cancellation applies.')
        return redirect('domains:portal_domain_detail', pk=pk)

    reason = (request.POST.get('reason') or '').strip()

    try:
        epp = begin_transfer_out(registration, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'Transfer-out flow failed for %s', registration.pk)
        messages.error(
            request,
            f'We couldn\'t start the transfer-out: {exc}. '
            f'We\'ve been notified.')
        return redirect('domains:portal_domain_detail', pk=pk)

    if epp:
        messages.success(
            request,
            'Cancellation started. We just emailed you the transfer-out '
            'package — including your auth code — and unlocked the '
            'domain for transfer.')
    else:
        messages.success(
            request,
            'Cancellation started. Your transfer auth code will arrive '
            'in your email shortly (some TLDs send it as a separate '
            'verification email).')
    return redirect('domains:portal_domain_detail', pk=pk)
