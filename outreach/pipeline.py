"""
Lead import pipeline — bridges raw scraper output to persisted Lead rows.

Flow per raw record:
    suppression check → dedup → score → create Lead

Returns a small summary dict the admin dashboard surfaces after a run.
"""

import logging

from .deduplication import is_duplicate
from .models import Lead, SuppressionList
from .scoring import score_lead


logger = logging.getLogger(__name__)


def import_leads(scraped_data, source):
    """
    Take a list of raw scraper dicts, score and dedup each, save the survivors.

    Args:
        scraped_data: list of dicts from a scraper (or any source — manual
            import, CSV upload, etc.) with keys like firm_name, city, state,
            email, phone, website, google_rating, etc.
        source: one of Lead.SOURCE_CHOICES values
                ('google_maps', 'state_bar', 'manual', etc.)

    Returns:
        dict with keys: total, imported, duplicates, suppressed, errors
    """
    results = {
        'total': len(scraped_data),
        'imported': 0,
        'duplicates': 0,
        'suppressed': 0,
        'errors': 0,
    }

    for raw in scraped_data:
        try:
            firm_name = (raw.get('firm_name') or '').strip()
            if not firm_name:
                # firm_name is required on the model; skip rather than crash.
                results['errors'] += 1
                continue

            # 1. Suppression check (permanent do-not-contact)
            email = (raw.get('email') or '').strip().lower()
            if email and SuppressionList.objects.filter(email=email).exists():
                results['suppressed'] += 1
                continue

            # 2. Dedup
            if is_duplicate(
                firm_name,
                (raw.get('city') or '').strip(),
                (raw.get('state') or '').strip(),
            ):
                results['duplicates'] += 1
                continue

            # 3. Score
            score, temperature = score_lead(raw)

            # 4. Create
            Lead.objects.create(
                firm_name=firm_name,
                attorney_name=(raw.get('attorney_name') or '').strip(),
                practice_area=(raw.get('practice_area') or '').strip(),
                business_type=(raw.get('business_type') or 'Law Firm').strip(),
                email=email,
                phone=(raw.get('phone') or '').strip(),
                website=(raw.get('website') or '').strip(),
                address=(raw.get('address') or '').strip(),
                city=(raw.get('city') or '').strip(),
                state=(raw.get('state') or '').strip(),
                google_rating=raw.get('google_rating'),
                google_review_count=raw.get('google_review_count') or 0,
                has_google_business=bool(raw.get('has_google_business', False)),
                score=score,
                temperature=temperature,
                source=source,
            )
            results['imported'] += 1

        except Exception:
            logger.exception(
                'Lead import failed for record: %s',
                raw.get('firm_name', '<no name>'),
            )
            results['errors'] += 1
            continue

    return results
