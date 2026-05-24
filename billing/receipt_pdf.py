"""
Branded PDF receipt for the OnboardingInvoice.

WeasyPrint with a .html fallback (matches the existing pattern in
`clients/pdf_utils.py`, `clients/proposal_pdf.py`, and
`reporting/scan_runner.py` — Windows dev / fresh servers without the
native cairo/pango libs fall back to HTML, which downstream code
serves transparently).
"""

import logging
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone


logger = logging.getLogger(__name__)


def generate_invoice_receipt_pdf(invoice):
    """
    Render the receipt PDF for the given OnboardingInvoice and write
    it to MEDIA_ROOT/receipts/<client_id>/<invoice_id>.{pdf,html}.

    Sets `invoice.receipt_pdf_path` to the relative path on success.
    Best-effort: a render failure logs and returns None rather than
    raising, so the calling webhook never blocks on a missing PDF.
    """
    client = invoice.client

    # Snapshot line items as plain floats for the template — easier
    # to format than JSON-stringified Decimals.
    line_items = [
        {'description': it.get('description', ''),
         'amount': float(it.get('amount', 0) or 0)}
        for it in (invoice.line_items or [])
    ]

    paid_at = invoice.paid_at or timezone.now()
    html_string = render_to_string(
        'billing/receipt_pdf.html',
        {
            'invoice': invoice,
            'client': client,
            'line_items': line_items,
            'total_amount': float(invoice.total_amount),
            'paid_at': paid_at,
            'rendered_at': timezone.now(),
        },
    )

    rel_dir = Path('receipts') / str(client.id)
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    rel_pdf = rel_dir / f'receipt-{invoice.id}.pdf'
    abs_pdf = Path(settings.MEDIA_ROOT) / rel_pdf

    try:
        from weasyprint import HTML
        HTML(string=html_string).write_pdf(str(abs_pdf))
        saved_rel = str(rel_pdf).replace('\\', '/')
    except Exception:
        # Fallback path — Windows dev / fresh server without cairo.
        # Save the HTML at the same name with .html so downstream
        # email-attach + admin-download logic can find it generically.
        logger.exception(
            'WeasyPrint failed for receipt %s — writing HTML fallback',
            invoice.pk)
        rel_html = rel_dir / f'receipt-{invoice.id}.html'
        (Path(settings.MEDIA_ROOT) / rel_html).write_text(
            html_string, encoding='utf-8')
        saved_rel = str(rel_html).replace('\\', '/')

    invoice.receipt_pdf_path = saved_rel
    invoice.save(update_fields=['receipt_pdf_path', 'updated_at'])
    return saved_rel
