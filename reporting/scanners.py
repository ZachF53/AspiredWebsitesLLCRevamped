"""
Vulnerability scanner wrappers — one function per tool.

Every scanner here follows the same contract:

  - takes the target (IP, URL, or domain) plus an optional timeout
  - shells out / API-calls to the tool
  - returns a `dict` that ALWAYS includes a `findings` list (possibly
    empty); may include `error`, `raw_output`, tool-specific summary
    fields
  - NEVER raises — callers can drop the result straight into a model
    JSONField without try/except

`normalize_findings` converts those raw finding dicts into the shape
`VulnerabilityFinding.objects.bulk_create` expects.
"""

import json
import logging
import os
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)


# ── Port-scan severity table — referenced from both run_nmap_scan and
# the report so a port → severity decision lives in one place.
DANGEROUS_PORTS = {
    '21': ('FTP open', 'medium',
           'FTP transmits credentials and data in plaintext. Disable '
           'if unused, or move to SFTP.'),
    '23': ('Telnet open', 'high',
           'Telnet is unencrypted — disable immediately and use SSH.'),
    '3306': ('MySQL exposed', 'high',
             'MySQL port is publicly accessible. Restrict to localhost.'),
    '5432': ('PostgreSQL exposed', 'high',
             'PostgreSQL port is publicly accessible. Restrict to '
             'localhost.'),
    '6379': ('Redis exposed', 'critical',
             'Redis has no auth by default. Restrict to localhost '
             'immediately or set requirepass.'),
    '27017': ('MongoDB exposed', 'critical',
              'MongoDB is publicly accessible. Restrict to localhost.'),
}


# ── nmap ────────────────────────────────────────────────────────────────────

def run_nmap_scan(target_ip, timeout=120):
    """
    Service-version + default-script TCP scan against `target_ip`.
    Parses the XML output and flags any port in DANGEROUS_PORTS as a
    finding with a baked-in remediation.
    """
    try:
        result = subprocess.run(
            ['nmap', '-sV', '-sC', '--open',
             '-T4', '--max-retries', '2',
             '-oX', '-', target_ip],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': 'nmap scan timed out',
                'ports': [], 'findings': []}
    except FileNotFoundError:
        return {'error': 'nmap not installed',
                'ports': [], 'findings': [], 'skipped': True}
    except Exception as exc:  # noqa: BLE001 — defensive
        return {'error': str(exc), 'ports': [], 'findings': []}

    if result.returncode != 0:
        return {'error': (result.stderr or '')[:500],
                'ports': [], 'findings': []}

    ports = []
    findings = []
    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError:
        return {'ports': [], 'findings': [],
                'raw_output': result.stdout[:5000],
                'error': 'nmap XML output unparseable'}

    for host in root.findall('host'):
        for port in host.findall('.//port'):
            state = port.find('state')
            if state is None or state.get('state') != 'open':
                continue
            service = port.find('service')
            portid = port.get('portid', '')
            protocol = port.get('protocol', '')
            svc_name = svc_version = svc_product = ''
            if service is not None:
                svc_name = service.get('name', '')
                svc_version = service.get('version', '')
                svc_product = service.get('product', '')
            ports.append({
                'port': portid, 'protocol': protocol,
                'service': svc_name, 'product': svc_product,
                'version': svc_version,
            })
            if portid in DANGEROUS_PORTS:
                title, sev, rec = DANGEROUS_PORTS[portid]
                findings.append({
                    'title': title, 'severity': sev,
                    'description': (f'Port {portid} ({svc_name}) is '
                                    f'publicly accessible.'),
                    'recommendation': rec,
                    'evidence': (f'nmap: port {portid}/{protocol} open — '
                                 f'{svc_product} {svc_version}'.strip()),
                })

    return {
        'ports': ports,
        'findings': findings,
        'raw_output': result.stdout[:5000],
    }


# ── Nikto ───────────────────────────────────────────────────────────────────

# Keyword → severity heuristic. Each match group is checked in order; the
# first non-info hit wins. Cheap-but-good-enough categorisation while we
# wait for the full CVE-aware Phase 6c Part 2 rewrite.
_NIKTO_SEVERITY = [
    ('critical', ('sql', 'injection', 'xss', 'cross-site',
                  'rce', 'execute', 'shell')),
    ('high', ('password', 'credential', 'auth', 'admin',
              'backup', 'config', 'phpinfo')),
    ('medium', ('outdated', 'version', 'header', 'cookie', 'csrf')),
    ('low', ('found', 'retrieved', 'exposed')),
]


def _classify_nikto_msg(msg):
    msg_lower = msg.lower()
    for sev, keywords in _NIKTO_SEVERITY:
        if any(k in msg_lower for k in keywords):
            return sev
    return 'info'


def run_nikto_scan(target_url, timeout=180):
    """
    Run Nikto against `target_url` and parse its findings.

    Ubuntu ships nikto 2.1.5 whose `-Format` option only supports
    `htm` / `nbe` / `xml` — there is NO json output, and `-Format` is
    silently ignored unless paired with `-output <file>`. The earlier
    `-Format json` invocation therefore produced text-on-stdout that
    our JSON parser skipped on every line → 0 findings, regardless of
    what nikto actually found. (See diagnosis trail on Food Trucks
    scan f2a463f8 in the Phase 6c notes.)

    The fix: write XML to a tempfile via `-output -Format xml`, parse
    `<item>` elements, and capture forensic fields (return code,
    truncated stdout/stderr, raw item count) on the returned dict so
    a future "why is X empty?" can be answered from the saved row.
    """
    if not target_url.startswith('http'):
        target_url = f'https://{target_url}'

    fd, xml_path = tempfile.mkstemp(suffix='.xml', prefix='nikto-')
    os.close(fd)

    try:
        result = subprocess.run(
            ['nikto', '-h', target_url,
             '-output', xml_path,
             '-Format', 'xml',
             '-nointeractive',
             '-Tuning', '1234567890',
             '-maxtime', '120s'],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': 'Nikto scan timed out', 'findings': []}
    except FileNotFoundError:
        return {'error': 'Nikto not installed',
                'findings': [], 'skipped': True}
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        return {'error': str(exc), 'findings': []}
    else:
        try:
            return _parse_nikto_xml(xml_path, result)
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass


def _parse_nikto_xml(xml_path, result):
    """
    Parse the XML file nikto wrote and combine with the subprocess
    result so callers can debug 'no findings' outcomes from the row.
    """
    findings = []
    item_count = 0
    parse_error = None
    try:
        tree = ET.parse(xml_path)
        items = list(tree.findall('.//item'))
        item_count = len(items)
        for item in items:
            msg = (item.findtext('description') or '').strip()
            if not msg:
                continue
            findings.append({
                'title': msg[:200],
                'severity': _classify_nikto_msg(msg),
                'description': f'Nikto found: {msg}',
                'recommendation': ('Review this finding and remediate '
                                   'if applicable.'),
                'evidence': (
                    f"URL: {item.findtext('uri', '')} "
                    f"Method: {item.get('method', 'GET')} "
                    f"OSVDB: {item.get('osvdbid', '')} "
                    f"ID: {item.get('id', '')}"),
            })
    except (ET.ParseError, FileNotFoundError) as exc:
        parse_error = str(exc)

    out = {
        'findings': findings,
        'item_count': item_count,
        'returncode': result.returncode,
        # Forensic fields — kept short so the JSONField doesn't bloat.
        'raw_stdout': (result.stdout or '')[:5000],
        'raw_stderr': (result.stderr or '')[:2000],
    }
    if parse_error:
        out['error'] = f'Nikto XML unparseable: {parse_error}'
    elif result.returncode not in (0, 1):
        # nikto 2.1.5 returns 1 when it finishes with findings — that's
        # not an error. Anything else is worth surfacing.
        out['error'] = f'Nikto exit {result.returncode}'
    return out


# ── SSL Labs ────────────────────────────────────────────────────────────────

_SSL_GRADE_FINDINGS = {
    'F': ('critical',
          'SSL/TLS configuration is critically insecure.',
          'Immediate remediation required — review and update the '
          'SSL configuration.'),
    'C': ('high',
          'SSL/TLS grade is C — significant weaknesses.',
          'Update to TLS 1.2+ only with strong cipher suites.'),
    'B': ('medium',
          'SSL/TLS grade is B — minor weaknesses detected.',
          'Review the SSL Labs report for specific issues to raise '
          'the grade.'),
    'A': ('info',
          'SSL/TLS grade is A — good configuration.',
          'No action required. Monitor for changes.'),
    # 'A+' is treated as 'A' via the first-letter lookup below.
}


def _strip_to_domain(value):
    if not value:
        return ''
    return value.replace('https://', '').replace(
        'http://', '').split('/')[0]


def run_ssl_scan(domain, timeout=30, max_poll_attempts=18):
    """
    SSL Labs grade check via their public API (no key, ~60–90s for a
    fresh scan). Polls every 10s up to `max_poll_attempts` times. Adds
    a Critical / High finding for SSLv2 / SSLv3 / RC4 if detected.
    """
    domain = _strip_to_domain(domain)
    if not domain:
        return {'error': 'No domain provided',
                'findings': [], 'grade': None}

    api_base = 'https://api.ssllabs.com/api/v3'

    try:
        resp = requests.get(
            f'{api_base}/analyze',
            params={'host': domain, 'startNew': 'on', 'all': 'done'},
            timeout=timeout)
    except requests.RequestException as exc:
        return {'error': str(exc), 'findings': [], 'grade': None}
    if resp.status_code != 200:
        return {'error': f'SSL Labs API error: {resp.status_code}',
                'findings': [], 'grade': None}

    data = resp.json() or {}

    # Poll until READY or ERROR (or we run out of patience).
    attempts = 0
    while (data.get('status') not in ('READY', 'ERROR')
           and attempts < max_poll_attempts):
        time.sleep(10)
        attempts += 1
        try:
            resp = requests.get(
                f'{api_base}/analyze',
                params={'host': domain, 'all': 'done'},
                timeout=timeout)
            if resp.status_code == 200:
                data = resp.json() or {}
        except requests.RequestException:
            continue

    if data.get('status') == 'ERROR':
        return {'error': data.get('statusMessage', 'SSL Labs error'),
                'findings': [], 'grade': None}

    endpoints = data.get('endpoints') or []
    if not endpoints:
        return {'grade': None, 'findings': [], 'raw_data': data,
                'error': 'SSL Labs returned no endpoints'}

    endpoint = endpoints[0]
    grade = endpoint.get('grade') or 'Unknown'
    findings = []

    grade_key = grade[0] if grade else 'F'
    if grade_key in _SSL_GRADE_FINDINGS:
        sev, desc, rec = _SSL_GRADE_FINDINGS[grade_key]
        if sev != 'info':
            findings.append({
                'title': f'SSL/TLS Grade: {grade}',
                'severity': sev,
                'description': desc,
                'recommendation': rec,
                'evidence': f'SSL Labs grade: {grade} for {domain}',
            })

    details = endpoint.get('details') or {}

    if details.get('supportsSsl2'):
        findings.append({
            'title': 'SSLv2 supported',
            'severity': 'critical',
            'description': 'SSLv2 is critically insecure and deprecated.',
            'recommendation': ('Disable SSLv2 immediately in the nginx '
                               'SSL configuration.'),
            'evidence': 'SSL Labs detected SSLv2 support',
        })
    if details.get('supportsSsl3'):
        findings.append({
            'title': 'SSLv3 supported',
            'severity': 'high',
            'description': 'SSLv3 is insecure (POODLE vulnerability).',
            'recommendation': 'Disable SSLv3 in the nginx configuration.',
            'evidence': 'SSL Labs detected SSLv3 support',
        })
    if details.get('supportsRc4'):
        findings.append({
            'title': 'RC4 cipher supported',
            'severity': 'high',
            'description': 'RC4 is a broken cipher and should not be used.',
            'recommendation': 'Disable RC4 in the nginx SSL configuration.',
            'evidence': 'SSL Labs detected RC4 support',
        })

    for chain in (details.get('certChains') or []):
        issues = chain.get('issues', 0)
        if issues:
            findings.append({
                'title': 'SSL certificate chain issues',
                'severity': 'medium',
                'description': (f'Certificate chain has {issues} '
                                f'issue(s).'),
                'recommendation': ('Check certificate chain validity '
                                   'and intermediate certs.'),
                'evidence': f'SSL Labs issues flag: {issues}',
            })

    return {'grade': grade, 'findings': findings, 'raw_data': data}


# ── WPScan ─────────────────────────────────────────────────────────────────

def _is_wordpress(target_url):
    """Cheap GET to spot WordPress before paying for the full WPScan."""
    try:
        resp = requests.get(target_url, timeout=15)
    except requests.RequestException:
        return False
    body = resp.text or ''
    return any(marker in body for marker in
               ('wp-content', 'wp-json', 'WordPress'))


def run_wpscan(target_url, timeout=120):
    """
    WPScan against `target_url`. Skips with `skipped=True` if the
    target doesn't look like WordPress.
    """
    if not target_url.startswith('http'):
        target_url = f'https://{target_url}'

    if not _is_wordpress(target_url):
        return {'findings': [], 'skipped': True,
                'reason': 'WordPress not detected on this site'}

    try:
        result = subprocess.run(
            ['wpscan', '--url', target_url,
             '--format', 'json',
             '--no-update',
             '--disable-tls-checks'],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': 'WPScan timed out', 'findings': []}
    except FileNotFoundError:
        return {'error': 'WPScan not installed',
                'findings': [], 'skipped': True}
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc), 'findings': []}

    findings = []
    try:
        data = json.loads(result.stdout or '{}')
    except (json.JSONDecodeError, TypeError):
        return {'findings': [], 'is_wordpress': True,
                'raw_output': (result.stdout or '')[:3000],
                'error': 'WPScan JSON unparseable'}

    # WordPress core vulnerabilities
    for vuln in (data.get('version') or {}).get('vulnerabilities') or []:
        cves = ((vuln.get('references') or {}).get('cve') or [])
        findings.append({
            'title': (f"WordPress vulnerability: "
                      f"{vuln.get('title', 'Unknown')}"),
            'severity': 'high',
            'description': vuln.get('title', ''),
            'recommendation': 'Update WordPress to the latest version.',
            'evidence': f"CVE: {', '.join(cves)}",
            'cve_id': ', '.join(cves),
        })

    # Plugin vulnerabilities
    for plugin_name, plugin_data in (data.get('plugins') or {}).items():
        for vuln in plugin_data.get('vulnerabilities') or []:
            cves = ((vuln.get('references') or {}).get('cve') or [])
            findings.append({
                'title': (f"Plugin vulnerability ({plugin_name}): "
                          f"{vuln.get('title', '')}"),
                'severity': 'high',
                'description': vuln.get('title', ''),
                'recommendation': (f'Update or remove plugin: '
                                   f'{plugin_name}'),
                'evidence': f'Plugin: {plugin_name}',
                'cve_id': ', '.join(cves),
            })

    return {
        'findings': findings,
        'is_wordpress': True,
        'raw_output': (result.stdout or '')[:3000],
    }


# ── Normaliser ─────────────────────────────────────────────────────────────

def normalize_findings(raw_findings, tool):
    """
    Convert a scanner's raw finding dicts into the canonical shape
    `VulnerabilityFinding.objects.bulk_create` consumes.
    """
    normalised = []
    for f in raw_findings:
        normalised.append({
            'tool': tool,
            'title': (f.get('title') or 'Unknown Finding')[:300],
            'severity': f.get('severity', 'info'),
            'description': f.get('description', ''),
            'recommendation': f.get('recommendation', ''),
            'evidence': f.get('evidence', ''),
            'cve_id': f.get('cve_id', ''),
        })
    return normalised
