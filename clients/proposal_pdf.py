"""
WeasyPrint renderer for sales Proposals (Phase 7 Part 2).

Mirrors the pattern in `clients/pdf_utils.py` (contracts): falls back
to a `.html` file when WeasyPrint isn't installed or its native deps
are missing. Returns a path relative to MEDIA_ROOT either way so the
caller can `proposal.pdf_path = render_proposal_pdf(proposal)`
unconditionally.

The HTML template lives at `clients/templates/clients/proposal.html`
and uses the orange + dark brand tokens from CLAUDE.md.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


# Print stylesheet — kept here (not in main.css) so WeasyPrint reads
# only what it needs and we don't fight cascading dark-theme variables.
_PDF_CSS = """
@page { size: letter; margin: 0.6in; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; color: #1A1A1A;
       line-height: 1.55; font-size: 11pt; margin: 0; }
h1 { font-size: 28pt; color: #E8650A; margin: 0 0 6px 0; }
h2 { font-size: 20pt; color: #1A1A1A; margin: 0 0 8px 0; }
h3 { font-size: 13pt; color: #1A1A1A; margin: 12px 0 4px 0; }
p, li { font-size: 11pt; }
.cover { page-break-after: always; background: #1A1A1A; color: #FFFFFF;
         padding: 1.6in 0.6in; min-height: 9.4in; }
.cover h1 { color: #E8650A; font-size: 30pt; }
.cover .business { font-size: 22pt; margin: 24px 0 36px 0;
                   color: #FFFFFF; }
.cover .meta { font-size: 11pt; color: #B5B5B5; line-height: 1.9; }
.cover .meta strong { color: #FFFFFF; }
.page { padding: 0.4in 0.2in; page-break-after: always; }
.page:last-of-type { page-break-after: auto; }
.eyebrow { display: inline-block; font-size: 9.5pt;
           letter-spacing: 0.16em; text-transform: uppercase;
           color: #E8650A; font-weight: 700; margin-bottom: 6px; }
.section { margin-top: 14px; }
.section p { margin: 0 0 8px 0; }
.bullet-list { margin: 0; padding-left: 1.1em; }
.bullet-list li { margin-bottom: 4px; }
.pricing { border: 1px solid #E5E5E5; border-radius: 8px;
           padding: 18px 22px; margin: 14px 0; }
.pricing__row { display: flex; justify-content: space-between;
                padding: 8px 0; border-bottom: 1px solid #F0F0F0; }
.pricing__row:last-child { border-bottom: 0;
                           padding-top: 12px; font-weight: 700;
                           font-size: 13pt; color: #E8650A; }
.pricing__row .label { color: #444444; }
.pricing__row .amount { color: #1A1A1A; }
.case { border-left: 3px solid #E8650A; padding: 8px 0 8px 14px;
        margin: 14px 0; page-break-inside: avoid; }
.case h3 { margin: 0 0 6px 0; }
.case .meta { color: #666666; font-size: 10pt; margin-bottom: 6px; }
.metrics { display: flex; gap: 14px; margin: 8px 0; }
.metrics .m { background: #FFF6EE; border: 1px solid #FAD9BB;
              border-radius: 6px; padding: 10px 14px; flex: 1; }
.metrics .m strong { display: block; font-size: 18pt;
                     color: #E8650A; }
.metrics .m span { font-size: 9pt; color: #666666;
                   text-transform: uppercase; letter-spacing: 0.06em; }
.quote { font-style: italic; color: #444444; margin: 8px 0;
         padding: 6px 0 6px 12px; border-left: 2px solid #DDDDDD; }
.quote .who { display: block; margin-top: 4px; font-style: normal;
              color: #666666; font-size: 9.5pt; }
.cta { background: #1A1A1A; color: #FFFFFF; border-radius: 8px;
       padding: 22px 24px; margin-top: 16px; }
.cta h2 { color: #E8650A; margin-bottom: 8px; }
.cta p, .cta a { color: #FFFFFF; }
"""


def render_proposal_pdf(proposal):
    """
    Render `proposal` to a PDF at media/proposals/<id>/proposal.pdf.

    Returns the path relative to MEDIA_ROOT. Falls back to a `.html`
    file when WeasyPrint is unavailable (Windows dev or a fresh server
    that hasn't run the WeasyPrint native-deps step yet).
    """
    from clients.models import CaseStudy

    # Resolve case studies once, in declared order.
    case_studies = []
    if proposal.case_study_ids:
        by_id = {
            str(cs.id): cs for cs in CaseStudy.objects.filter(
                id__in=proposal.case_study_ids)
        }
        case_studies = [by_id[str(i)] for i in proposal.case_study_ids
                        if str(i) in by_id]

    # Pull feature bullets for the selected build package, if any —
    # graceful when ServiceTier doesn't have a row matching the
    # free-text `package` field (proposals often use display names
    # like "Premium Build + Growth Maintenance").
    feature_bullets = _resolve_feature_bullets(proposal.package)

    html = render_to_string('clients/proposal.html', {
        'p': proposal,
        'today': timezone.now().date(),
        'case_studies': case_studies,
        'feature_bullets': feature_bullets,
    })

    timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
    rel_dir = Path('proposals') / str(proposal.id)
    abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    rel_pdf = rel_dir / f'proposal_{timestamp}.pdf'
    try:
        from weasyprint import CSS, HTML
        HTML(string=html).write_pdf(
            target=str(Path(settings.MEDIA_ROOT) / rel_pdf),
            stylesheets=[CSS(string=_PDF_CSS)],
        )
        return str(rel_pdf).replace('\\', '/')
    except Exception:
        logger.exception('WeasyPrint failed for proposal %s', proposal.pk)
        rel_html = rel_dir / f'proposal_{timestamp}.html'
        # Inline the CSS so the .html fallback is self-contained
        # (someone might open it in a browser if SendGrid attaches it).
        wrapped = (
            f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<style>{_PDF_CSS}</style></head><body>{html}</body></html>'
        )
        (Path(settings.MEDIA_ROOT) / rel_html).write_text(
            wrapped, encoding='utf-8')
        return str(rel_html).replace('\\', '/')


def _resolve_feature_bullets(package_text):
    """
    Best-effort lookup of bullet features for a free-text package
    label. Tries exact `name__iexact` first, then a substring match.
    Returns a list of strings (possibly empty).
    """
    if not package_text:
        return []
    try:
        from billing.pricing_models import ServiceTier
        tier = (ServiceTier.objects
                .filter(name__iexact=package_text).first()
                or ServiceTier.objects
                .filter(name__icontains=package_text.split()[0]).first())
        if tier is None:
            return []
        return [f.text for f in tier.features.all() if f.text]
    except Exception:
        return []
