"""Celery tasks for the outreach pipeline."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def enrich_lead_task(self, lead_id):
    """
    Background lead enrichment — fired by ``import_leads`` for every
    new lead saved during a scrape, and by the "Re-enrich" admin
    button on the lead detail page.

    Wraps ``outreach.enricher.enrich_lead`` so the slow HTTP work
    (homepage fetch + PageSpeed + Custom Search) happens off the
    request thread. Bounded retries with a 2-minute delay handle
    transient network blips; permanent failures fall through to the
    enrichment_log on the lead.
    """
    from outreach.enricher import enrich_lead
    from outreach.models import Lead

    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning('enrich_lead_task: lead %s not found', lead_id)
        return

    try:
        enrich_lead(lead)
    except Exception as exc:  # noqa: BLE001
        logger.exception('enrich_lead_task crashed for %s', lead_id)
        # Retry once for transient errors. After retries exhausted the
        # exception propagates and Celery logs it — but enrich_lead's
        # own per-step try/except has already written the partial
        # state to enrichment_log so the admin can see what went wrong.
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            pass
