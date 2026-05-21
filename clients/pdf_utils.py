"""Contract PDF rendering via WeasyPrint."""

import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# Print stylesheet — the contract HTML carries no CSS of its own.
_PDF_CSS = """
@page { size: letter; margin: 1in; }
body { font-family: Arial, Helvetica, sans-serif; color: #1A1A1A;
       line-height: 1.55; font-size: 11pt; }
h1 { font-size: 18pt; color: #E8650A; margin-bottom: 2px; }
h2 { font-size: 12.5pt; margin-top: 18px; margin-bottom: 4px; }
p, li { font-size: 11pt; }
.contract-doc__meta { color: #666666; font-size: 9.5pt; margin-top: 0; }
.contract-doc__sigblock { margin-top: 22px; }
.contract-sig-record { margin-top: 28px; border-top: 1px solid #cccccc;
                       padding-top: 12px; font-size: 9.5pt; color: #444444; }
"""


def render_contract_pdf(contract):
    """
    Render a signed contract to a PDF under MEDIA_ROOT/contracts/<client_id>/.

    Returns the path relative to MEDIA_ROOT. If WeasyPrint is unavailable
    (e.g. missing native libraries on Windows), falls back to writing the
    contract as an .html file so the signed record is still persisted, and
    returns that path instead.
    """
    timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
    rel_dir = Path('contracts') / str(contract.client_id)
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    signed_at = contract.signed_at.strftime('%Y-%m-%d %H:%M:%S %Z') if contract.signed_at else ''
    sig_record = (
        '<div class="contract-sig-record">'
        f'<strong>Signature record</strong><br>'
        f'Signed by: {contract.signed_name}<br>'
        f'Date: {signed_at}<br>'
        f'IP address: {contract.signed_ip or "unknown"}'
        '</div>'
    )
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        f'{contract.contract_text}{sig_record}</body></html>'
    )

    rel_pdf = rel_dir / f'contract_{timestamp}.pdf'
    try:
        from weasyprint import CSS, HTML
        HTML(string=html).write_pdf(
            target=str(Path(settings.MEDIA_ROOT) / rel_pdf),
            stylesheets=[CSS(string=_PDF_CSS)],
        )
        return str(rel_pdf).replace('\\', '/')
    except Exception:
        logger.exception('WeasyPrint PDF render failed for contract %s', contract.pk)
        rel_html = rel_dir / f'contract_{timestamp}.html'
        (Path(settings.MEDIA_ROOT) / rel_html).write_text(html, encoding='utf-8')
        return str(rel_html).replace('\\', '/')
