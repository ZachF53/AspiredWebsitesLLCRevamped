"""
DigitalOcean droplet dashboard.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names, so urls.py — which references them as
`views.<name>` — keeps working unchanged.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from django.conf import settings


# ────────────────────────────────────────────────────────────────────────────
# Phase 6b — Droplet dashboard
# ────────────────────────────────────────────────────────────────────────────

DROPLET_REGIONS = [
    ('nyc1', 'NYC1 — New York'),
    ('nyc3', 'NYC3 — New York 3'),
    ('sfo3', 'SFO3 — San Francisco'),
    ('ams3', 'AMS3 — Amsterdam'),
    ('sgp1', 'SGP1 — Singapore'),
    ('lon1', 'LON1 — London'),
    ('fra1', 'FRA1 — Frankfurt'),
    ('tor1', 'TOR1 — Toronto'),
    ('blr1', 'BLR1 — Bangalore'),
]

DROPLET_SIZES = [
    {'slug': 's-1vcpu-1gb', 'vcpus': 1, 'memory_gb': 1,
     'disk_gb': 25, 'price': 6},
    {'slug': 's-1vcpu-2gb', 'vcpus': 1, 'memory_gb': 2,
     'disk_gb': 50, 'price': 12},
    {'slug': 's-2vcpu-2gb', 'vcpus': 2, 'memory_gb': 2,
     'disk_gb': 60, 'price': 18},
    {'slug': 's-2vcpu-4gb', 'vcpus': 2, 'memory_gb': 4,
     'disk_gb': 80, 'price': 24},
]


def _droplet_rows(droplets, websites_by_ip):
    """
    Decorate raw DO droplet dicts with the dashboard display fields.

    `is_client_droplet` is True if EITHER the DO tag list contains
    'client' OR the IP matches a Website.do_droplet_ip. The IP-match arm
    matters for legacy Droplets that pre-date the tagging convention
    — without it the Destroy button would arm for every real client
    server (footgun).

    Each row carries:
      - ``website`` — the Website this droplet belongs to. Source of
                      truth for "what is this droplet for".
      - ``client``  — kept as an alias of ``website`` so the existing
                      template column keeps rendering.
      - ``is_unlinked`` — True when no Website points at this IP.
                      Drives the "Link to Website" action.
    """
    rows = []
    for d in droplets:
        linked_website = websites_by_ip.get(d['ip'])
        linked_client = linked_website
        tag_says_client = 'client' in (d.get('tags') or [])
        is_client = bool(tag_says_client or linked_website)
        is_manual = (not is_client) or 'manual' in (d.get('tags') or [])
        is_unlinked = not linked_website
        if d['status'] == 'active':
            border = 'green'
        elif d['status'] in ('off', 'archive'):
            border = 'orange'
        else:
            border = 'red'
        rows.append({
            **d,
            'client': linked_client,
            'website': linked_website,
            'is_client_droplet': is_client,
            'is_manual_droplet': is_manual,
            'is_unlinked': is_unlinked,
            'monthly_cost_str': f"${d['monthly_cost']:.0f}/mo",
            'border': border,
        })
    return rows


def _load_droplet_dashboard():
    """Pull DO droplets + match them to Websites by IP. Pure read."""
    from billing.do_helpers import get_all_droplets
    from clients.account_models import Website

    droplets = get_all_droplets()
    websites_by_ip = {
        w.do_droplet_ip: w
        for w in Website.objects.select_related('account').filter(
            do_droplet_ip__isnull=False)
        if w.do_droplet_ip
    }
    rows = _droplet_rows(droplets, websites_by_ip)
    return {
        'rows': rows,
        'total_count': len(rows),
        'active_count': sum(1 for r in rows if r['status'] == 'active'),
        'total_cost': sum(r['monthly_cost'] for r in rows),
        'unlinked_count': sum(1 for r in rows if r['is_unlinked']),
    }


@admin_required
def droplet_list(request):
    """Full Droplet dashboard — stats + table."""
    from clients.account_models import Website
    data = _load_droplet_dashboard()
    # Pre-fetched options for the "Link to Website" modal — one
    # query for the list page, not per-row. Excludes websites that
    # already have a droplet so admin can't accidentally clobber.
    link_targets = list(
        Website.objects.select_related('account')
        .filter(do_droplet_id='')
        .order_by('account__name', 'name'))
    return render(request, 'admin_dashboard/droplets_list.html',
                  _admin_context(
                      'droplets',
                      rows=data['rows'],
                      total_count=data['total_count'],
                      active_count=data['active_count'],
                      total_cost=data['total_cost'],
                      unlinked_count=data['unlinked_count'],
                      link_targets=link_targets,
                      base_snapshot_id=getattr(
                          settings, 'DO_BASE_SNAPSHOT_ID', ''),
                  ))


@admin_required
def droplet_table(request):
    """HTMX partial — just the table rows, polled every 30s on the list page."""
    data = _load_droplet_dashboard()
    return render(request, 'admin_dashboard/_droplet_rows.html', {
        'rows': data['rows'],
    })


@admin_required
def droplet_new(request):
    """
    Render the spin-up form on GET; on POST enqueue the Celery provisioning
    task and redirect back to the list with a notice. The list page then
    HTMX-polls until the new Droplet shows up active.
    """
    from billing.do_helpers import next_droplet_name
    from clients.account_models import Website

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        region = (request.POST.get('region') or 'nyc1').strip()
        size = (request.POST.get('size') or 's-1vcpu-1gb').strip()
        client_id = (request.POST.get('client_id') or '').strip() or None

        # Tag based on client linkage — display-time logic mirrors this.
        tags = ['aspired-websites', 'client' if client_id else 'manual']

        from billing.tasks import provision_manual_droplet_task
        provision_manual_droplet_task.delay(
            name=name or next_droplet_name('manual'),
            region=region,
            size=size,
            snapshot_id=int(settings.DO_BASE_SNAPSHOT_ID)
            if settings.DO_BASE_SNAPSHOT_ID else None,
            tags=tags,
            client_id=client_id,
        )
        return redirect(
            f"{reverse('admin_dashboard:droplet_list')}?provisioning={name}")

    # GET — render the form. next_droplet_name() is a live API call, so
    # protect the form against API outages.
    try:
        suggested_name = next_droplet_name('manual')
    except Exception:  # noqa: BLE001 — never block the page
        suggested_name = 'manual-001'

    clients = (Website.objects.select_related('account')
               .order_by('account__name', 'name'))

    return render(request, 'admin_dashboard/droplets_new.html', _admin_context(
        'droplets',
        suggested_name=suggested_name,
        regions=DROPLET_REGIONS,
        sizes=DROPLET_SIZES,
        clients=clients,
        base_snapshot_id=getattr(settings, 'DO_BASE_SNAPSHOT_ID', ''),
    ))


@admin_required
@require_POST
def droplet_power(request, droplet_id):
    """
    Power a Droplet on or off. Body: action=on | off. Returns the refreshed
    row partial so the dashboard updates inline via HTMX.
    """
    from billing.do_helpers import (
        get_droplet, power_off_droplet, power_on_droplet,
    )
    from clients.account_models import Website

    action = (request.POST.get('action') or '').strip()
    if action == 'on':
        ok = power_on_droplet(droplet_id)
    elif action == 'off':
        ok = power_off_droplet(droplet_id)
    else:
        return HttpResponseBadRequest('action must be "on" or "off"')

    if not ok:
        return HttpResponseBadRequest('DO action failed')

    # Re-fetch for the row refresh (DO is async, so status may still be
    # transitioning — that's fine, the table will keep polling).
    d = get_droplet(droplet_id)
    if d is None:
        return HttpResponseBadRequest('Droplet not found')

    websites_by_ip = {
        w.do_droplet_ip: w
        for w in Website.objects.select_related('account').filter(
            do_droplet_ip=d['ip'])
        if w.do_droplet_ip
    }
    rows = _droplet_rows([d], websites_by_ip)
    return render(request, 'admin_dashboard/_droplet_rows.html', {
        'rows': rows, 'single_row': True,
    })


@admin_required
def droplet_destroy(request, droplet_id):
    """
    Destroy a Droplet. GET shows the confirm modal; POST validates the
    typed-name match + refuses if client-tagged + clears the linked
    Website's IP if any.
    """
    from billing.do_helpers import destroy_droplet, get_droplet
    from clients.account_models import Website
    from django.contrib import messages

    d = get_droplet(droplet_id)
    if d is None:
        from django.http import Http404
        raise Http404('Droplet not found')

    linked_client = (Website.objects
                     .select_related('account')
                     .filter(do_droplet_ip=d['ip'])
                     .first())
    # Same rule as the list-row gate: 'client' tag OR a real client linkage
    # via matching IP protects the Droplet. Legacy client Droplets predate
    # the tag convention, so the IP arm matters in production.
    is_client_droplet = (
        'client' in (d.get('tags') or []) or linked_client is not None)

    if request.method == 'POST':
        if is_client_droplet:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden(
                "Refusing to destroy this Droplet — it is either tagged "
                "'client' or linked to a ClientProfile by IP. "
                "Unlink the client first.")
        typed = (request.POST.get('confirm_name') or '').strip()
        if typed != d['name']:
            messages.error(
                request, "Name did not match — Droplet was NOT destroyed.")
            return redirect('admin_dashboard:droplet_destroy',
                            droplet_id=droplet_id)
        if not destroy_droplet(droplet_id):
            messages.error(
                request, f"DigitalOcean refused to destroy '{d['name']}'.")
            return redirect('admin_dashboard:droplet_list')
        if linked_client:
            linked_client.do_droplet_id = ''
            linked_client.do_droplet_ip = None
            linked_client.save(update_fields=[
                'do_droplet_id', 'do_droplet_ip', 'updated_at'])
        messages.success(
            request, f"Droplet '{d['name']}' has been destroyed.")
        return redirect('admin_dashboard:droplet_list')

    return render(request, 'admin_dashboard/droplets_destroy.html',
                  _admin_context(
                      'droplets',
                      droplet=d,
                      is_client_droplet=is_client_droplet,
                      linked_client=linked_client,
                  ))


@admin_required
@require_POST
def droplet_link_to_website(request, droplet_id):
    """
    Attach a DigitalOcean droplet to a specific Website. Used when a
    droplet got orphaned (e.g. its old Website was deleted, or the
    droplet was created out-of-band) and needs to be re-linked.

    Mechanics:
      - Reads the droplet's current state from DO (id, ip, name).
      - Clears do_droplet_* on ANY other Website pointing at this
        droplet (a droplet can only belong to one site at a time).
      - Writes the droplet metadata onto the picked Website.
      - Mirrors to that website's legacy ClientProfile so the old
        droplet-dashboard match path keeps working too.

    Refuses to attach to a Website that already has a different
    droplet_id — admin must clear the existing link first to avoid
    silent overwrites.
    """
    from django.contrib import messages

    from billing.do_helpers import get_droplet
    from clients.account_models import Website

    website_id = (request.POST.get('website_id') or '').strip()
    if not website_id:
        messages.error(request, 'Pick a website to link the droplet to.')
        return redirect('admin_dashboard:droplet_list')

    website = Website.objects.filter(id=website_id).first()
    if website is None:
        messages.error(request, 'Website not found.')
        return redirect('admin_dashboard:droplet_list')

    # Refuse to clobber an existing droplet binding on the target.
    if website.do_droplet_id and website.do_droplet_id != str(droplet_id):
        messages.error(
            request,
            f'{website.name} already has droplet '
            f'{website.do_droplet_id}. Clear that linkage on the '
            f'Website page first.')
        return redirect('admin_dashboard:droplet_list')

    # Pull fresh droplet state from DO — guards against the dashboard
    # showing stale info.
    droplet = get_droplet(droplet_id)
    if droplet is None:
        messages.error(
            request,
            'Could not fetch droplet from DigitalOcean — it may have '
            'been destroyed already, or the API is down.')
        return redirect('admin_dashboard:droplet_list')

    droplet_id_str = str(droplet['id'])
    droplet_ip = droplet.get('ip') or ''
    droplet_name = droplet.get('name') or ''

    # If some OTHER Website already points at this droplet, unlink
    # it there first — a droplet binds to exactly one site.
    other_sites = Website.objects.filter(
        do_droplet_id=droplet_id_str).exclude(id=website.id)
    unlinked_names = []
    for ws in other_sites:
        unlinked_names.append(ws.name)
        ws.do_droplet_id = ''
        ws.do_droplet_ip = None
        ws.do_droplet_name = ''
        ws.save(update_fields=[
            'do_droplet_id', 'do_droplet_ip', 'do_droplet_name',
            'updated_at'])

    # Write the linkage onto the target Website.
    website.do_droplet_id = droplet_id_str
    website.do_droplet_ip = droplet_ip or None
    website.do_droplet_name = droplet_name
    website.save(update_fields=[
        'do_droplet_id', 'do_droplet_ip', 'do_droplet_name',
        'updated_at'])

    # The legacy mirror is gone with the dashboard's legacy match path:
    # the droplet IP lives on the Website and nothing reads it off the
    # profile any more.

    msg = (f'Droplet "{droplet_name}" ({droplet_ip}) linked to '
           f'{website.name}.')
    if unlinked_names:
        msg += (f' Also cleared old linkage on: '
                f'{", ".join(unlinked_names)}.')
    messages.success(request, msg)
    return redirect('admin_dashboard:droplet_list')


@admin_required
def droplet_metrics(request, droplet_id):
    """
    Per-Droplet metrics — DO API basics + (if vault unlocked + we have an
    SSH credential) live supervisor/disk/memory/uptime over SSH.
    Uptime stats come from the existing UptimeRecord table.
    """
    from billing.do_helpers import get_droplet
    from clients.account_models import Website
    from reporting.uptime_helpers import (
        get_avg_response_time, get_current_status, get_uptime_percentage,
    )
    from vault.models import VaultCredential
    from vault.views import get_vault_key

    d = get_droplet(droplet_id)
    if d is None:
        from django.http import Http404
        raise Http404('Droplet not found')

    client = (Website.objects
              .select_related('account')
              .filter(do_droplet_ip=d['ip'])
              .first())

    cred = None
    if d['ip']:
        # Credentials hang off the account's vault; the droplet IP
        # identifies which of that account's sites we are looking at.
        cred = VaultCredential.objects.filter(
            is_ssh_credential=True,
            vault__account_new__websites__do_droplet_ip=d['ip']).first()

    vault_key = get_vault_key(request)
    ssh_metrics = None
    if cred and vault_key is not None and not cred.encrypted_with_server_key:
        ssh_metrics = _fetch_ssh_metrics(cred, vault_key)

    uptime_30 = uptime_avg_ms = uptime_status = None
    uptime_website = None
    if client:
        try:
            _acct = client.migrated_account
        except Exception:
            _acct = None
        if _acct is not None:
            uptime_website = _acct.websites.order_by('created_at').first()
        scope = uptime_website or client
        uptime_30 = get_uptime_percentage(scope, 30)
        uptime_avg_ms = get_avg_response_time(scope, 30)
        uptime_status = get_current_status(scope)

    return render(request, 'admin_dashboard/droplets_metrics.html',
                  _admin_context(
                      'droplets',
                      droplet=d,
                      client=client,
                      uptime_website=uptime_website,
                      cred=cred,
                      vault_unlocked=vault_key is not None,
                      ssh_metrics=ssh_metrics,
                      uptime_30=uptime_30,
                      uptime_avg_ms=uptime_avg_ms,
                      uptime_status=uptime_status,
                  ))


def _fetch_ssh_metrics(cred, vault_key):
    """
    Run a handful of read-only diagnostics over SSH. Returns a dict of
    {label: command_output} or None on connection failure. Each command
    is capped short — this is a dashboard view, not a long-running task.
    """
    import paramiko

    from vault.crypto import decrypt_value

    host = decrypt_value(cred.ssh_host_encrypted, vault_key)
    user = decrypt_value(cred.ssh_username_encrypted, vault_key)
    if not host or host == '[decryption failed]' or not user:
        return None

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        'hostname': host, 'port': cred.ssh_port or 22, 'username': user,
        'timeout': 10, 'allow_agent': False, 'look_for_keys': False,
    }
    if (cred.ssh_auth_type or 'password') == 'private_key':
        key_text = decrypt_value(cred.ssh_private_key_encrypted, vault_key)
        passphrase = (
            decrypt_value(cred.ssh_key_passphrase_encrypted, vault_key)
            if cred.ssh_key_passphrase_encrypted else None)
        from vault.consumers import _load_private_key
        try:
            connect_kwargs['pkey'] = _load_private_key(key_text, passphrase)
        except Exception:
            return None
    else:
        connect_kwargs['password'] = decrypt_value(
            cred.ssh_password_encrypted, vault_key)

    commands = [
        ('supervisor', 'supervisorctl status'),
        ('disk', 'df -h /'),
        ('memory', 'free -h'),
        ('uptime', 'uptime'),
        ('gunicorn_errors',
         'tail -5 /var/www/aspired/logs/gunicorn-error.log 2>/dev/null'
         ' || echo "(no log)"'),
    ]
    out = {}
    try:
        ssh.connect(**connect_kwargs)
        for label, cmd in commands:
            try:
                _, stdout, _ = ssh.exec_command(cmd, timeout=8)
                out[label] = stdout.read().decode(
                    'utf-8', errors='replace').strip()
            except Exception:
                out[label] = '(failed)'
    except Exception:
        return None
    finally:
        try:
            ssh.close()
        except Exception:
            pass
    return out


