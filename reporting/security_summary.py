"""
Monthly 1-page security summary — a standalone per-site PDF emailed on
the 1st to every site on a paid plan (reporting.security_eligibility).
Deliberately separate from the multi-page performance MonthlyReport.

The PDF always has the same five sections the pricing page promises:

1. Dependency vulnerabilities — pip-audit over the site's virtualenvs
   (custom builds, from the SSH droplet health check) or WPScan's
   vulnerable core / plugin / theme findings (WordPress, from the
   external scan).
2. File integrity — sha256 manifest diff vs the accepted baseline
   (droplet health check; Aspired-hosted sites only).
3. SSL certificate — live certificate validity / expiry plus the SSL
   Labs grade (external scan).
4. Security headers — which recommended response headers the live site
   sends (external scan).
5. Uptime — the reported month's uptime % and incident count
   (clients.models UptimeRecord / UptimeAlert).

A section that cannot be produced says so in plain words ("not
available for sites not hosted on Aspired servers", "could not be
completed this month") rather than disappearing.

Only a scan / droplet check completed within FRESHNESS_DAYS of the send
date is reported. Anything older is treated as "this month's scan could
not complete": the summary still goes out, flags it, and the admin is
alerted (see run_security_summaries).

Entry points:
- generate_security_summary(website, report_month) — build + render one
  site's report row (idempotent per site/month).
- run_security_summaries(...) — the whole monthly run; used by the
  Celery beat task and the `send_security_summaries` management command.
"""

import logging
import os
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from clients.display import owner_label
from reporting.models import (
    DropletHealthCheck, SecuritySummaryReport, VulnerabilityScan,
)

logger = logging.getLogger(__name__)

# A scan / droplet check older than this (relative to the send date) is
# not reported — the section says the check could not complete instead.
FRESHNESS_DAYS = 35

NOT_HOSTED_MSG = 'Not available for sites not hosted on Aspired servers.'
SCAN_MISSING_MSG = ("This month's external scan could not be completed. "
                    "We have been alerted and are re-running it.")
SERVER_CHECK_MISSING_MSG = ("This month's server check could not be "
                            "completed. We have been alerted and are "
                            "re-running it.")

# Section order + the plain-English "what this is" line for each.
SECTION_META = (
    ('dependencies', 'Dependency vulnerabilities',
     'We check the software packages your site is built on against '
     'public databases of known security flaws.'),
    ('file_integrity', 'File integrity',
     "We fingerprint every file of your site's code and flag any file "
     'that was added, removed or changed without us putting it there.'),
    ('ssl', 'SSL certificate',
     'The certificate that keeps the padlock in the address bar and '
     'encrypts traffic between visitors and your site.'),
    ('headers', 'Security headers',
     'Instructions your site sends to browsers that block common '
     'attacks such as clickjacking and script injection.'),
    ('uptime', 'Uptime',
     'We check that your site is reachable every few minutes, around '
     'the clock.'),
)


# ── Time helpers ──────────────────────────────────────────────────────────

def _month_start(d):
    return date(d.year, d.month, 1)


def _next_month(d):
    d = _month_start(d)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _aware_midnight(d):
    return timezone.make_aware(datetime.combine(d, time.min))


def previous_month(today=None):
    """First day of the month before `today` (default: local today) —
    the month the 1st-of-month send reports on."""
    today = today or timezone.localdate()
    first = _month_start(today)
    return _month_start(first - timedelta(days=1))


def summary_reference_time(report_month, now=None):
    """The moment freshness is measured from: the start of the month
    after `report_month` (the send date), or now if that is still in
    the future (a preview / manual run for the current month)."""
    now = now or timezone.now()
    return min(now, _aware_midnight(_next_month(report_month)))


def latest_fresh(qs, reference):
    """Newest ``status='complete'`` row in `qs` completed within
    FRESHNESS_DAYS before `reference`, or None."""
    return (qs.filter(status='complete',
                      completed_at__gte=reference - timedelta(
                          days=FRESHNESS_DAYS))
            .order_by('-completed_at').first())


def site_has_droplet(website):
    """True for sites we host (custom build on an Aspired Droplet) —
    the only ones droplet-only checks can run against."""
    return bool(website.do_droplet_ip) and website.build_platform != 'wordpress'


# ── Section builders (pure over their inputs) ─────────────────────────────

def _section(key, status, headline, items=None):
    return {'key': key, 'status': status, 'headline': headline,
            'items': list(items or [])[:5]}


def _pip_audit_items(raw):
    items = []
    for venv_data in ((raw or {}).get('venvs') or {}).values():
        result = (venv_data or {}).get('result')
        deps = (result.get('dependencies') if isinstance(result, dict)
                else result) or []
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            for vuln in dep.get('vulns') or []:
                fix = ', '.join(vuln.get('fix_versions') or [])
                items.append(
                    f"{dep.get('name')} {dep.get('version')} — "
                    f"{vuln.get('id')}"
                    + (f' (fixed in {fix})' if fix else ''))
    return items


def build_dependency_section(website, scan, droplet_check):
    if website.build_platform == 'wordpress':
        if scan is None:
            return _section('dependencies', 'unavailable', SCAN_MISSING_MSG)
        raw = scan.raw_wpscan or {}
        if not raw or raw.get('skipped') or raw.get('error'):
            reason = raw.get('reason') or raw.get('error') or 'not run'
            return _section(
                'dependencies', 'unavailable',
                'WordPress core, plugins and themes could not be checked '
                f'this month ({reason}).')
        open_wp = list(scan.findings.filter(tool='wpscan', status='open')
                       .values_list('title', flat=True))
        if not open_wp:
            return _section(
                'dependencies', 'good',
                'No known vulnerabilities in WordPress core, plugins or '
                'themes.')
        return _section(
            'dependencies', 'attention',
            f'{len(open_wp)} known vulnerabilit'
            f'{"y" if len(open_wp) == 1 else "ies"} in WordPress core, '
            'plugins or themes. We are updating the affected components.',
            open_wp)

    if droplet_check is None:
        return _section('dependencies', 'unavailable',
                        SERVER_CHECK_MISSING_MSG if site_has_droplet(website)
                        else NOT_HOSTED_MSG)
    raw = droplet_check.raw_pip_audit or {}
    count = droplet_check.pip_audit_vulnerability_count
    if raw.get('skipped') or count is None:
        return _section(
            'dependencies', 'unavailable',
            'The dependency audit could not run this month'
            + (f" ({raw.get('reason')})." if raw.get('reason') else '.'))
    if count == 0:
        return _section('dependencies', 'good',
                        'No known vulnerabilities in the packages your '
                        'site runs on.')
    return _section(
        'dependencies', 'attention',
        f'{count} known vulnerabilit{"y" if count == 1 else "ies"} in '
        'installed packages. We are applying the fixed versions.',
        _pip_audit_items(raw))


def build_file_integrity_section(website, droplet_check):
    if droplet_check is None:
        return _section('file_integrity', 'unavailable',
                        SERVER_CHECK_MISSING_MSG if site_has_droplet(website)
                        else NOT_HOSTED_MSG)
    raw = droplet_check.raw_file_integrity or {}
    status = raw.get('status')
    n = raw.get('file_count') or 0
    if status == 'baseline_recorded':
        return _section(
            'file_integrity', 'good',
            f'Baseline recorded: {n} files fingerprinted. From next month '
            'any change to them is flagged here.')
    if status == 'clean':
        return _section('file_integrity', 'good',
                        f'No unexpected changes across {n} files.')
    if status == 'changed':
        added = raw.get('added_count', 0)
        removed = raw.get('removed_count', 0)
        changed = raw.get('changed_count', 0)
        items = ([f'Added: {p}' for p in raw.get('added') or []]
                 + [f'Changed: {p}' for p in raw.get('changed') or []]
                 + [f'Removed: {p}' for p in raw.get('removed') or []])
        return _section(
            'file_integrity', 'attention',
            f'{changed} changed, {added} added, {removed} removed since '
            'the last reviewed baseline. Changes after updates we deploy '
            'are expected; we review every one.', items)
    return _section('file_integrity', 'unavailable',
                    'The file integrity check could not be completed this '
                    'month. We have been alerted.')


def build_ssl_section(scan):
    if scan is None:
        return _section('ssl', 'unavailable', SCAN_MISSING_MSG)
    grade = (scan.raw_ssl or {}).get('grade')
    cert = (scan.raw_http or {}).get('certificate') or {}
    grade_txt = f' SSL Labs grade: {grade}.' if grade else ''

    if cert.get('valid') and cert.get('not_after'):
        days = cert.get('days_remaining')
        try:
            expires = datetime.fromisoformat(cert['not_after'])
            exp_txt = f'{expires:%B} {expires.day}, {expires.year}'
        except (TypeError, ValueError):
            exp_txt = cert['not_after']
        status = 'good'
        if days is not None and days < 14:
            status = 'attention'
        if grade and grade[:1].upper() not in ('A',):
            status = 'attention'
        return _section(
            'ssl', status,
            f'Valid, renews automatically. Expires {exp_txt} '
            f'({days} days from the scan).{grade_txt}')
    if cert.get('error'):
        return _section('ssl', 'attention',
                        f"Certificate problem: {cert['error']}. We are "
                        'fixing it.')
    if grade:
        status = 'good' if grade[:1].upper() == 'A' else 'attention'
        return _section('ssl', status, f'Certificate in place.{grade_txt}')
    return _section('ssl', 'unavailable',
                    'The certificate check could not be completed this '
                    'month.')


def build_headers_section(scan):
    if scan is None:
        return _section('headers', 'unavailable', SCAN_MISSING_MSG)
    headers = (scan.raw_http or {}).get('headers')
    if not headers:
        return _section('headers', 'unavailable',
                        'The security header check could not be completed '
                        'this month.')
    present = headers.get('present') or []
    missing = headers.get('missing') or []
    total = len(present) + len(missing)
    if not missing:
        return _section('headers', 'good',
                        f'All {total} recommended headers are in place.')
    return _section(
        'headers', 'attention',
        f'{len(present)} of {total} recommended headers in place. '
        f'Missing: {", ".join(missing)}.')


def build_uptime_section(uptime_percent, incident_count):
    if uptime_percent is None:
        return _section('uptime', 'unavailable',
                        'No uptime checks were recorded for this month.')
    incidents = (f'{incident_count} outage'
                 f'{"" if incident_count == 1 else "s"}')
    status = ('good' if uptime_percent >= 99.5 and incident_count == 0
              else 'attention')
    return _section('uptime', status,
                    f'{uptime_percent:g}% uptime, {incidents} this month.')


def month_uptime(website, report_month):
    """``(uptime_percent, incident_count)`` for the calendar month.
    uptime_percent is None when there were no checks at all."""
    from clients.models import UptimeAlert, UptimeRecord

    start = _aware_midnight(_month_start(report_month))
    end = _aware_midnight(_next_month(report_month))
    records = UptimeRecord.objects.filter(
        website_new=website, checked_at__gte=start, checked_at__lt=end)
    total = records.count()
    incidents = UptimeAlert.objects.filter(
        website_new=website, alerted_at__gte=start,
        alerted_at__lt=end).count()
    if total == 0:
        return None, incidents
    up = records.filter(is_up=True).count()
    return round(up / total * 100, 2), incidents


def _overall_status(open_critical, open_high, disk_percent,
                     pending_updates, pip_vulns, services_down):
    """Simple, documented heuristic — red on anything actively broken
    or exploitable, yellow on anything that needs attention but isn't
    urgent, green otherwise."""
    if open_critical > 0 or services_down > 0:
        return 'red'
    if disk_percent is not None and disk_percent >= 90:
        return 'red'
    if open_high > 0 or pip_vulns:
        return 'yellow'
    if pending_updates is not None and pending_updates > 10:
        return 'yellow'
    return 'green'


def _combine_status(base, sections, scan_stale):
    """Fold the section results into the scan/server heuristic: any
    section needing attention or a stale scan lifts green to yellow;
    an invalid certificate is red."""
    if base == 'red':
        return 'red'
    ssl = sections.get('ssl') or {}
    if ssl.get('status') == 'attention' and ssl.get('headline', '').startswith(
            'Certificate problem'):
        return 'red'
    if scan_stale or any(s.get('status') == 'attention'
                         for s in sections.values()):
        return 'yellow'
    return base


# ── Report generation ─────────────────────────────────────────────────────

def collect_summary_data(website, report_month, now=None):
    """Everything the summary needs, with no writes. Used by the report
    generator and by the command's --dry-run."""
    report_month = _month_start(report_month)
    reference = summary_reference_time(report_month, now)
    scan = latest_fresh(
        VulnerabilityScan.objects.filter(website_new=website), reference)
    droplet_check = None
    # Any fresh completed server check is honoured, even if the site's
    # droplet IP has since been cleared; WordPress sites never have one.
    if website.build_platform != 'wordpress':
        droplet_check = latest_fresh(
            DropletHealthCheck.objects.filter(website_new=website),
            reference)
    return {
        'report_month': report_month,
        'reference': reference,
        'scan': scan,
        'droplet_check': droplet_check,
        'scan_stale': scan is None,
        'server_check_stale': (site_has_droplet(website)
                               and droplet_check is None),
    }


def generate_security_summary(website, report_month, now=None):
    """
    Create/update this website's SecuritySummaryReport for
    `report_month` (any date within the month — normalised to the 1st),
    render the PDF, and return the report row.

    Idempotent per (website, report_month) — unique_together on the
    model means a re-run updates the same row rather than creating a
    duplicate, same pattern as MonthlyReport.
    """
    data = collect_summary_data(website, report_month, now)
    report_month = data['report_month']
    scan, droplet_check = data['scan'], data['droplet_check']

    report, _ = SecuritySummaryReport.objects.get_or_create(
        website_new=website, report_month=report_month,
        defaults={'status': 'generating'})

    open_critical = open_high = 0
    if scan is not None:
        open_critical = scan.findings.filter(
            severity='critical', status='open').count()
        open_high = scan.findings.filter(
            severity='high', status='open').count()

    disk_percent = pending_updates = pip_vulns = None
    services_down_count = 0
    if droplet_check is not None:
        disk_percent = droplet_check.disk_usage_percent
        pending_updates = droplet_check.pending_os_updates_count
        pip_vulns = droplet_check.pip_audit_vulnerability_count
        services_down_count = len(droplet_check.services_down or [])

    uptime_percent, incident_count = month_uptime(website, report_month)

    sections = {
        'dependencies': build_dependency_section(
            website, scan, droplet_check),
        'file_integrity': build_file_integrity_section(
            website, droplet_check),
        'ssl': build_ssl_section(scan),
        'headers': build_headers_section(scan),
        'uptime': build_uptime_section(uptime_percent, incident_count),
    }
    for key, title, explanation in SECTION_META:
        sections[key]['title'] = title
        sections[key]['explanation'] = explanation

    base = _overall_status(
        open_critical, open_high, disk_percent, pending_updates,
        pip_vulns or 0, services_down_count)
    overall = _combine_status(base, sections, data['scan_stale'])

    owner_name = owner_label(website)
    month_str = report_month.strftime('%B %Y')

    html_string = render_to_string('reporting/security_summary.html', {
        'owner_name': owner_name,
        'site_url': website.url,
        'month_str': month_str,
        'overall_status': overall,
        'scan': scan,
        'scan_stale': data['scan_stale'],
        'droplet_check': droplet_check,
        'open_critical_count': open_critical,
        'open_high_count': open_high,
        'disk_usage_percent': disk_percent,
        'pending_os_updates_count': pending_updates,
        'services_down_count': services_down_count,
        'is_hosted': site_has_droplet(website) or droplet_check is not None,
        'sections': [sections[k] for k, _, _ in SECTION_META],
    })

    rel_dir = os.path.join('security-summaries', str(website.id))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    date_slug = report_month.strftime('%Y-%m')
    pdf_filename = f'summary-{date_slug}.pdf'
    rel_pdf_path = os.path.join(rel_dir, pdf_filename)
    abs_pdf_path = os.path.join(abs_dir, pdf_filename)

    rendered_rel = rel_pdf_path
    try:
        from weasyprint import HTML
        HTML(string=html_string,
             base_url=settings.MEDIA_ROOT).write_pdf(abs_pdf_path)
    except Exception as exc:  # noqa: BLE001 — WeasyPrint / GTK / lib gaps
        logger.warning(
            'WeasyPrint failed for security summary %s (%s) — falling '
            'back to HTML', report.id, exc)
        html_filename = f'summary-{date_slug}.html'
        rendered_rel = os.path.join(rel_dir, html_filename)
        with open(os.path.join(abs_dir, html_filename),
                  'w', encoding='utf-8') as fh:
            fh.write(html_string)

    report.pdf_path = rendered_rel
    if report.status != 'sent':
        report.status = 'ready'
    report.latest_scan = scan
    report.latest_droplet_check = droplet_check
    report.open_critical_count = open_critical
    report.open_high_count = open_high
    report.disk_usage_percent = disk_percent
    report.pending_os_updates_count = pending_updates
    report.pip_audit_vulnerability_count = pip_vulns
    report.services_down_count = services_down_count
    report.overall_status = overall
    report.uptime_percent = uptime_percent
    report.uptime_incident_count = incident_count
    report.scan_stale = data['scan_stale']
    report.sections = sections
    report.save()
    return report


# ── The monthly run ───────────────────────────────────────────────────────

def _recipient(website):
    account = website.account
    user = getattr(account, 'user', None) if account else None
    return (getattr(user, 'email', '') or '') if user else ''


def _email_text(first_name, month_str, report, site_name):
    status_line = {
        'green': 'Everything checked out this month.',
        'yellow': 'A few items need attention; the details are in the '
                  'attached summary.',
        'red': 'We found issues that need prompt attention; the details '
               'are in the attached summary.',
    }.get(report.overall_status, '')
    return (
        f'Hi {first_name},\n\n'
        f'Your one-page security summary for {site_name} for {month_str} '
        f'is attached.\n\n'
        f'{status_line}\n\n'
        'Each month we scan your site and check five things: the software '
        'it depends on for known vulnerabilities, its files for '
        'unexpected changes, its SSL certificate, its security headers, '
        'and its uptime.\n\n'
        'Fixes for any issues we find are covered by your plan. There is '
        'nothing extra to pay and nothing you need to do.\n\n'
        f'Past summaries are in your portal: '
        f'{settings.SITE_BASE_URL}/portal/security/\n\n'
        'Questions? Just reply to this email.\n\n'
        '— The Aspired Websites team\n'
    )


def _send_summary_email(website, report, recipient):
    from clients.emails import send_branded

    account = website.account
    month_str = report.report_month.strftime('%B %Y')
    contact_name = (account.contact_name or account.name) if account else ''
    first_name = (contact_name or '').split(' ')[0] or 'there'
    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path)
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = 'application/pdf' if ext.lower() == '.pdf' else 'text/html'
    with open(abs_path, 'rb') as fh:
        pdf_bytes = fh.read()

    send_branded(
        subject=f'Your Security Summary — {month_str} — {website.name}',
        template='security_summary_monthly',
        context={
            'name': first_name,
            'site_name': website.name,
            'month_str': month_str,
            'overall_status': report.overall_status,
            'sections': [report.sections.get(k) or {}
                         for k, _, _ in SECTION_META],
            'security_url': f'{settings.SITE_BASE_URL}/portal/security/',
            'preheader': f'Your security summary for {month_str}.',
        },
        recipient_list=[recipient],
        text_body=_email_text(first_name, month_str, report, website.name),
        from_email=getattr(settings, 'EMAIL_FROM_NO_REPLY',
                           settings.DEFAULT_FROM_EMAIL),
        attachments=[
            (f'security-summary-{report.report_month:%Y-%m}{ext}',
             pdf_bytes, mime)],
        fail_silently=False,
    )


def _alert_admin(report_month, problems):
    """One SystemAlert per problem site + one admin email listing all."""
    if not problems:
        return
    from django.core.mail import send_mail

    from core.system_alerts import record_alert

    month_str = report_month.strftime('%B %Y')
    for name, reason in problems:
        record_alert(
            severity='warning', source='reporting.security_summary',
            message=f'Security summary {month_str}: {name} — {reason}'[:255])
    body = (f'Monthly security summaries for {month_str} went out with '
            f'these problems:\n\n'
            + '\n'.join(f'  - {name}: {reason}' for name, reason in problems)
            + '\n\nRe-run the scan / server check from the admin dashboard, '
              'then regenerate with:\n'
              '  python manage.py send_security_summaries '
              f'--month {report_month:%Y-%m} --website <uuid>\n')
    try:
        send_mail(
            f'[Security summary] {len(problems)} site(s) need attention '
            f'— {month_str}',
            body,
            getattr(settings, 'EMAIL_FROM_NO_REPLY',
                    settings.DEFAULT_FROM_EMAIL),
            [settings.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception('security summary admin alert email failed')


def run_security_summaries(report_month=None, website_ids=None,
                           dry_run=False, now=None, resend=False):
    """
    Generate and email the monthly security summary for every eligible
    site (reporting.security_eligibility). Shared by the beat task and
    the management command.

    - report_month: any date in the month to report on (default: the
      previous month).
    - website_ids: restrict to these Website ids (still must be
      eligible).
    - dry_run: no writes, no email — returns what WOULD happen.
    - resend: also re-send reports already marked sent for the month
      (the beat never does; only a manual run can).

    Returns a dict with per-site outcomes and totals.
    """
    from reporting.security_eligibility import security_report_websites

    report_month = _month_start(report_month or previous_month())
    sites = security_report_websites()
    if website_ids:
        sites = sites.filter(id__in=list(website_ids))

    result = {'report_month': report_month, 'dry_run': dry_run,
              'sites': [], 'sent': 0, 'failed': 0, 'skipped': 0}
    problems = []

    for site in sites:
        recipient = _recipient(site)
        row = {'website_id': str(site.id), 'name': site.name,
               'recipient': recipient}
        result['sites'].append(row)

        if dry_run:
            data = collect_summary_data(site, report_month, now)
            row.update({
                'scan': (data['scan'].completed_at.isoformat()
                         if data['scan'] else None),
                'droplet_check': (
                    data['droplet_check'].completed_at.isoformat()
                    if data['droplet_check'] else None),
                'scan_stale': data['scan_stale'],
                'server_check_stale': data['server_check_stale'],
                'hosted': site_has_droplet(site),
                'outcome': 'would_send' if recipient else 'no_email',
            })
            continue

        if not recipient:
            row['outcome'] = 'no_email'
            result['skipped'] += 1
            problems.append((site.name, 'no email address on the account'))
            continue

        existing = SecuritySummaryReport.objects.filter(
            website_new=site, report_month=report_month,
            status='sent').first()
        if existing is not None and not resend:
            row['outcome'] = 'already_sent'
            result['skipped'] += 1
            continue

        try:
            report = generate_security_summary(site, report_month, now)
        except Exception:  # noqa: BLE001
            logger.exception('security summary generation failed for %s',
                             site.id)
            row['outcome'] = 'generation_failed'
            result['failed'] += 1
            problems.append((site.name, 'summary generation failed'))
            continue

        if report.scan_stale:
            problems.append((site.name, 'no external scan completed in the '
                                        f'last {FRESHNESS_DAYS} days'))
        if site_has_droplet(site) and report.latest_droplet_check is None:
            problems.append((site.name, 'no server check completed in the '
                                        f'last {FRESHNESS_DAYS} days'))

        abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path)
        if not report.pdf_path or not os.path.exists(abs_path):
            row['outcome'] = 'file_missing'
            result['failed'] += 1
            problems.append((site.name, 'rendered summary missing on disk'))
            continue

        try:
            _send_summary_email(site, report, recipient)
        except Exception:  # noqa: BLE001
            logger.exception('security summary send failed for %s', site.id)
            row['outcome'] = 'send_failed'
            result['failed'] += 1
            problems.append((site.name, 'email send failed'))
            continue

        report.status = 'sent'
        report.sent_at = timezone.now()
        report.save(update_fields=['status', 'sent_at', 'updated_at'])
        row['outcome'] = 'sent'
        result['sent'] += 1

    if not dry_run:
        _alert_admin(report_month, problems)
    result['problems'] = problems
    return result
