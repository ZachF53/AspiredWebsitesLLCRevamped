"""
SSH-based in-depth Droplet health audit.

`run_droplet_health_check(check_id)` is the single entry point — same
role as reporting.scan_runner.run_full_scan, called by the Celery task
wrapper, the on-demand admin button, and (for testing) synchronously
from `manage.py shell`.

Unlike VulnerabilityScan (external black-box scanning against the
public IP/URL, run from the Celery worker host), this authenticates
INTO the droplet via vault.ssh_ops.open_automation_ssh and reads real
system state: disk/memory usage, service status, pending OS updates,
pip-audit dependency CVEs, basic security posture (ufw/fail2ban).

Custom-build sites only (build_platform != 'wordpress') — WordPress
sites have no Aspired-managed Droplet to SSH into. Guarded at every
call site, not just here.

The pip-audit step is deliberately non-mutating: it discovers venvs
under /var/www by finding `bin/pip` files rather than assuming a fixed
path (unconfirmed against a real client Droplet — see the module-level
note below), captures each venv's installed packages via `pip freeze`
(read-only), and audits that freeze output with a system-wide pip-audit
install — it never installs anything INTO a client's venv.

CONFIRMED against the aspired-base-v2 snapshot (this app's own staging
server, which is provisioned from the same base image): nginx,
postgresql, redis-server run as systemd services; supervisor manages
the application process(es). NOT YET CONFIRMED against an actual
client Droplet: the exact venv path / supervisor program name a
client's own custom-build app uses — probing avoids hardcoding a
guess. Every step is best-effort; a step that doesn't apply to a given
site (e.g. no venv found) is recorded as skipped, never a hard failure
for the whole check.
"""

import json
import logging

from django.utils import timezone

from reporting.models import DropletHealthCheck

logger = logging.getLogger(__name__)

# systemd services confirmed present on every Droplet from the base
# snapshot (CLAUDE.md: "Ubuntu LTS, Nginx, Python 3, Gunicorn,
# PostgreSQL, Redis, Certbot"). Not every site necessarily USES
# postgresql/redis, so an inactive service here is reported, not
# treated as automatically critical — the admin reads it in context.
_BASE_SYSTEMD_SERVICES = ['nginx', 'postgresql', 'redis-server']


def _check_disk(ssh):
    from vault.ssh_ops import run_remote
    code, out, err = run_remote(ssh, 'df -h /', check=False)
    raw = {'exit_code': code, 'stdout': out, 'stderr': err}
    percent = None
    # Second line, second-to-last column: "/dev/vda1  48G  6.4G  42G  14% /"
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) >= 2:
        parts = lines[1].split()
        for p in parts:
            if p.endswith('%'):
                try:
                    percent = int(p.rstrip('%'))
                except ValueError:
                    pass
                break
    return raw, percent


def _check_memory(ssh):
    from vault.ssh_ops import run_remote
    code, out, err = run_remote(ssh, 'free -m', check=False)
    return {'exit_code': code, 'stdout': out, 'stderr': err}


def _check_services(ssh):
    """systemd base services + a generic supervisorctl status parse —
    the latter doesn't assume any specific program name, since that
    convention isn't confirmed for a real client site."""
    from vault.ssh_ops import run_remote

    systemd_status = {}
    for svc in _BASE_SYSTEMD_SERVICES:
        code, out, _ = run_remote(
            ssh, f'systemctl is-active {svc}', check=False)
        systemd_status[svc] = (out or '').strip() or 'unknown'

    supervisor_status = {}
    code, out, err = run_remote(ssh, 'supervisorctl status', check=False)
    if code == 0 or out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                supervisor_status[parts[0]] = parts[1]

    down = [name for name, state in systemd_status.items()
            if state != 'active']
    down += [name for name, state in supervisor_status.items()
             if state != 'RUNNING']

    raw = {
        'systemd': systemd_status,
        'supervisor': supervisor_status,
        'supervisor_raw': out,
    }
    return raw, down


def _check_os_updates(ssh):
    from vault.ssh_ops import run_remote
    code, out, err = run_remote(
        ssh, 'apt list --upgradable 2>/dev/null', check=False)
    raw = {'exit_code': code, 'stdout': out, 'stderr': err}
    lines = [l for l in out.splitlines() if l.strip()]
    # First line is always "Listing..." when apt has output at all.
    count = max(len(lines) - 1, 0) if lines else 0
    return raw, count


def _check_security(ssh):
    from vault.ssh_ops import run_remote
    ufw_code, ufw_out, _ = run_remote(ssh, 'ufw status', check=False)
    f2b_code, f2b_out, _ = run_remote(
        ssh, 'systemctl is-active fail2ban', check=False)
    return {
        'ufw_active': 'active' in (ufw_out or '').lower(),
        'ufw_raw': ufw_out,
        'fail2ban_active': (f2b_out or '').strip() == 'active',
    }


def _discover_venvs(ssh):
    """Find candidate venvs under /var/www by locating `bin/pip` files,
    rather than assuming a fixed path — unconfirmed against a real
    client Droplet's actual layout. Returns a list of venv root paths."""
    from vault.ssh_ops import run_remote
    code, out, err = run_remote(
        ssh,
        "find /var/www -maxdepth 4 -type f -name pip -path '*/bin/*' "
        "2>/dev/null",
        check=False)
    venvs = []
    for line in out.splitlines():
        line = line.strip()
        if line.endswith('/bin/pip'):
            venvs.append(line[:-len('/bin/pip')])
    return venvs


def _check_pip_audit(ssh):
    """Non-mutating: freezes each discovered venv's installed packages
    (read-only) and audits that freeze output with a system-level
    pip-audit install — never installs anything into a client's venv."""
    from vault.ssh_ops import run_remote

    venvs = _discover_venvs(ssh)
    if not venvs:
        return {'skipped': True, 'reason': 'no venv found under /var/www'}, None

    # One system-level pip-audit install, reused across every venv this
    # run and every future run on this box.
    check_code, _, _ = run_remote(ssh, 'command -v pip-audit', check=False)
    if check_code != 0:
        install_code, install_out, install_err = run_remote(
            ssh, 'pip3 install --quiet --user pip-audit', check=False,
            timeout=90)
        if install_code != 0:
            return {
                'skipped': True,
                'reason': 'pip-audit install failed',
                'install_stdout': install_out,
                'install_stderr': install_err,
            }, None

    results = {}
    total_vulns = 0
    for venv in venvs:
        freeze_code, freeze_out, freeze_err = run_remote(
            ssh, f'{venv}/bin/python -m pip freeze --disable-pip-version-check',
            check=False)
        if freeze_code != 0 or not freeze_out.strip():
            results[venv] = {'skipped': True, 'reason': 'pip freeze failed',
                              'stderr': freeze_err}
            continue

        req_path = f'/tmp/aspired-pip-audit-{abs(hash(venv))}.txt'
        run_remote(
            ssh,
            f"cat > {req_path} << 'AUDITEOF'\n{freeze_out}AUDITEOF",
            check=False)
        audit_code, audit_out, audit_err = run_remote(
            ssh,
            f'~/.local/bin/pip-audit -r {req_path} --format json '
            f'--progress-spinner off',
            check=False, timeout=120)
        run_remote(ssh, f'rm -f {req_path}', check=False)

        try:
            parsed = json.loads(audit_out) if audit_out.strip() else {}
        except (ValueError, TypeError):
            parsed = {'raw': audit_out, 'parse_error': True}

        vuln_count = 0
        if isinstance(parsed, dict):
            deps = parsed.get('dependencies') or []
            vuln_count = sum(
                len(d.get('vulns') or []) for d in deps
                if isinstance(d, dict))
        elif isinstance(parsed, list):
            vuln_count = sum(
                len(d.get('vulns') or []) for d in parsed
                if isinstance(d, dict))

        total_vulns += vuln_count
        results[venv] = {
            'exit_code': audit_code, 'vulnerability_count': vuln_count,
            'result': parsed, 'stderr': audit_err,
        }

    return {'venvs': results}, total_vulns


def run_droplet_health_check(check_id):
    """Entry point. Opens SSH, runs every check best-effort, stores
    results, sets status. Returns the DropletHealthCheck row."""
    check = DropletHealthCheck.objects.get(id=check_id)
    check.status = 'running'
    check.started_at = timezone.now()
    check.save(update_fields=['status', 'started_at', 'updated_at'])

    website = check.website_new
    if website is None or website.build_platform == 'wordpress':
        check.status = 'failed'
        check.error_message = (
            'No website_new set, or build_platform is wordpress — '
            'this site has no Aspired-managed Droplet to SSH into.')
        check.completed_at = timezone.now()
        check.save(update_fields=[
            'status', 'error_message', 'completed_at', 'updated_at'])
        return check

    from vault.ssh_ops import open_automation_ssh
    ssh = open_automation_ssh(website)
    if ssh is None:
        check.status = 'ssh_unavailable'
        check.error_message = (
            'Could not open an automated SSH session — no automation-'
            'enabled credential for this account, or the connection '
            'failed. See vault/ssh_ops.py — if this credential has '
            'been opened in the vault UI before, re-save its edit '
            'form once to refresh automation access.')
        check.completed_at = timezone.now()
        check.save(update_fields=[
            'status', 'error_message', 'completed_at', 'updated_at'])
        return check

    try:
        raw_disk, disk_percent = _check_disk(ssh)
        raw_memory = _check_memory(ssh)
        raw_services, services_down = _check_services(ssh)
        raw_os_updates, updates_count = _check_os_updates(ssh)
        raw_pip_audit, vuln_count = _check_pip_audit(ssh)
        raw_security = _check_security(ssh)

        check.raw_disk = raw_disk
        check.raw_memory = raw_memory
        check.raw_services = raw_services
        check.raw_os_updates = raw_os_updates
        check.raw_pip_audit = raw_pip_audit
        check.raw_security = raw_security
        check.disk_usage_percent = disk_percent
        check.pending_os_updates_count = updates_count
        check.pip_audit_vulnerability_count = vuln_count
        check.services_down = services_down
        check.status = 'complete'
    except Exception as exc:  # noqa: BLE001 — surfaced on the check row
        logger.exception(
            'run_droplet_health_check: unexpected failure for check %s',
            check_id)
        check.status = 'failed'
        check.error_message = str(exc)[:2000]
    finally:
        try:
            ssh.close()
        except Exception:
            pass
        check.completed_at = timezone.now()
        check.save()

    return check
