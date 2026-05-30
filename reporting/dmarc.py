"""
DMARC aggregate (rua) report ingest.

Public entry points:
  - ``parse_dmarc_attachment(raw_bytes, filename='')`` — auto-detects
    .zip / .gz / .xml and returns the raw XML string. Use this for
    anything you fetch from an email attachment.
  - ``ingest_dmarc_xml(xml_string)`` — parses the XML and writes
    one DmarcReport + N DmarcRecord rows. Idempotent on report_id
    (re-ingesting the same report is a no-op). Returns the
    DmarcReport row (or None if already-ingested / unparseable).

The XML schema is RFC 7489 § 7.2 — every major provider emits
the same structure, just with their own metadata in ``<org_name>``.
"""

import gzip
import io
import logging
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


def parse_dmarc_attachment(raw_bytes, filename=''):
    """Unwrap a DMARC report attachment to its inner XML string.

    DMARC reports come compressed — Gmail wraps in .zip, Microsoft
    in .gz, Apple sometimes in either. This sniffs the magic bytes
    + filename extension to pick the right decoder.

    Returns the XML string, or '' when nothing usable was found.
    """
    if not raw_bytes:
        return ''
    fname = (filename or '').lower()

    # ZIP magic = PK\x03\x04
    if raw_bytes[:4] == b'PK\x03\x04' or fname.endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
                # Reports always contain a single .xml inside the zip.
                for name in z.namelist():
                    if name.lower().endswith('.xml'):
                        return z.read(name).decode(
                            'utf-8', errors='replace')
        except Exception:
            logger.exception('DMARC: zip unwrap failed for %r', filename)
            return ''

    # GZIP magic = 1f 8b
    if raw_bytes[:2] == b'\x1f\x8b' or fname.endswith('.gz'):
        try:
            return gzip.decompress(raw_bytes).decode(
                'utf-8', errors='replace')
        except Exception:
            logger.exception('DMARC: gz unwrap failed for %r', filename)
            return ''

    # Already plain XML
    try:
        text = raw_bytes.decode('utf-8', errors='replace')
        if text.lstrip().startswith('<?xml') or '<feedback' in text[:200]:
            return text
    except Exception:
        pass
    return ''


def ingest_dmarc_xml(xml_string):
    """Parse one DMARC XML report and persist it.

    Idempotent on report_metadata/report_id — re-ingesting the
    same report returns the existing row without changes. Wraps
    the whole parse + save in try/except so a malformed report
    never propagates a 500 to the upload form.

    Returns the DmarcReport row, or None on parse failure / duplicate.
    """
    from reporting.models import DmarcReport, DmarcRecord

    if not xml_string or not xml_string.strip():
        return None

    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as exc:
        logger.warning('DMARC: XML parse failed: %s', exc)
        return None

    # Schema is RFC 7489 §7.2 — no namespaces in the wild, just
    # plain tags. Use _find with a default '' so missing optional
    # children don't crash.

    def _text(node, path, default=''):
        if node is None:
            return default
        found = node.find(path)
        return (found.text or default).strip() if (
            found is not None and found.text is not None) else default

    def _int(node, path, default=0):
        try:
            return int(_text(node, path, str(default)))
        except (TypeError, ValueError):
            return default

    meta = root.find('report_metadata')
    if meta is None:
        return None
    report_id = _text(meta, 'report_id')
    if not report_id:
        return None

    # De-dup early — providers occasionally re-send the same report.
    existing = DmarcReport.objects.filter(report_id=report_id).first()
    if existing is not None:
        return existing

    org_name = _text(meta, 'org_name')
    org_email = _text(meta, 'email')
    date_range = meta.find('date_range')
    try:
        period_start = datetime.fromtimestamp(
            _int(date_range, 'begin'), tz=timezone.utc)
        period_end = datetime.fromtimestamp(
            _int(date_range, 'end'), tz=timezone.utc)
    except (OSError, ValueError):
        logger.warning(
            'DMARC: bad timestamps in %s — skipping', report_id)
        return None

    policy = root.find('policy_published')
    policy_domain = _text(policy, 'domain')
    policy_p = _text(policy, 'p')
    policy_pct = _int(policy, 'pct')

    # ── First pass: build records + tally aggregates ──
    records_to_create = []
    total_messages = 0
    dmarc_pass = dmarc_fail = 0
    dkim_pass = dkim_fail = 0
    spf_pass = spf_fail = 0

    for rec in root.findall('record'):
        row = rec.find('row')
        ids = rec.find('identifiers')
        auth = rec.find('auth_results')
        pe = row.find('policy_evaluated') if row is not None else None

        source_ip = _text(row, 'source_ip')
        # Defensive — IPv6 strings can contain whitespace from some
        # poorly-formatted reports. Validate with Django before save.
        if not source_ip:
            continue
        count = _int(row, 'count', 0)
        disposition = _text(pe, 'disposition', 'none')
        dkim_aligned = _text(pe, 'dkim', 'none')
        spf_aligned = _text(pe, 'spf', 'none')

        header_from = _text(ids, 'header_from')

        # auth_results.dkim / auth_results.spf — there may be multiple
        # of each; we take the first <dkim> and first <spf>.
        dkim_node = auth.find('dkim') if auth is not None else None
        spf_node = auth.find('spf') if auth is not None else None

        rec_dict = {
            'source_ip': source_ip,
            'count': count,
            'disposition': disposition,
            'dkim_aligned': dkim_aligned,
            'spf_aligned': spf_aligned,
            'header_from': header_from,
            'dkim_domain': _text(dkim_node, 'domain'),
            'dkim_selector': _text(dkim_node, 'selector'),
            'dkim_result': _text(dkim_node, 'result', 'none'),
            'spf_domain': _text(spf_node, 'domain'),
            'spf_result': _text(spf_node, 'result', 'none'),
        }
        records_to_create.append(rec_dict)

        # Aggregate tallies.
        total_messages += count
        # DMARC passes if EITHER aligned check passes.
        if dkim_aligned == 'pass' or spf_aligned == 'pass':
            dmarc_pass += count
        else:
            dmarc_fail += count
        if dkim_aligned == 'pass':
            dkim_pass += count
        else:
            dkim_fail += count
        if spf_aligned == 'pass':
            spf_pass += count
        else:
            spf_fail += count

    # ── Second pass: save the report + records in one transaction ──
    from django.db import transaction
    try:
        with transaction.atomic():
            report = DmarcReport.objects.create(
                org_name=org_name,
                org_email=org_email,
                report_id=report_id,
                period_start=period_start,
                period_end=period_end,
                policy_domain=policy_domain,
                policy_p=policy_p,
                policy_pct=policy_pct or None,
                total_messages=total_messages,
                dmarc_pass=dmarc_pass,
                dmarc_fail=dmarc_fail,
                dkim_pass=dkim_pass,
                dkim_fail=dkim_fail,
                spf_pass=spf_pass,
                spf_fail=spf_fail,
                raw_xml=xml_string[:200_000],  # cap at 200kb just in case
            )
            DmarcRecord.objects.bulk_create([
                DmarcRecord(report=report, **r) for r in records_to_create
            ])
    except Exception:
        logger.exception('DMARC: save failed for report %s', report_id)
        return None

    return report
