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


# ── Cold outreach automation ───────────────────────────────────────────────

@shared_task
def run_cold_sender_task():
    """
    Daily — generate the day's cold outreach batch. Honours warming
    cap + OutreachSettings.daily_send_cap. Each new EmailSent row is
    queued for approval OR auto-promoted to 'approved' based on the
    current trust level (see outreach.gating).
    """
    from outreach.sender import generate_pending_cold_emails

    try:
        counts = generate_pending_cold_emails()
    except Exception:  # noqa: BLE001
        logger.exception('cold sender crashed')
        return 'failed'
    return (
        f'considered={counts["considered"]} '
        f'generated={counts["generated"]} '
        f'skipped_cap={counts["skipped_cap"]} '
        f'skipped_ai={counts["skipped_ai"]}'
        + (f' reason={counts["reason"]}' if counts['reason'] else '')
    )


@shared_task
def send_approved_emails_task():
    """
    Every 30 minutes during business hours. Drains the 'approved'
    queue — actually dispatches via SendGrid SMTP and flips status
    to 'sent'.
    """
    from outreach.dispatcher import dispatch_approved_batch

    try:
        counts = dispatch_approved_batch()
    except Exception:  # noqa: BLE001
        logger.exception('send drainer crashed')
        return 'failed'
    return (
        f'sent={counts["sent"]} failed={counts["failed"]} '
        f'suppressed={counts["suppressed"]}')


@shared_task
def reset_daily_counters_task():
    """
    Midnight — reset OutreachSettings.emails_sent_today. The counter
    is informational (the cap math reads EmailSent rows directly);
    keeping it for the dashboard so 'today' resets visibly.
    """
    from django.utils import timezone

    from outreach.models import OutreachSettings

    cfg = OutreachSettings.load()
    cfg.emails_sent_today = 0
    cfg.last_reset_date = timezone.localdate()
    cfg.save(update_fields=['emails_sent_today', 'last_reset_date'])
    return 'reset ok'


# ── Inbound reply pipeline ─────────────────────────────────────────────────

@shared_task
def ingest_replies_task():
    """Every 15 min — poll IMAP, write EmailReply rows, fan out classify."""
    from outreach.reply_ingest import ingest_replies

    try:
        counts = ingest_replies()
    except Exception:  # noqa: BLE001
        logger.exception('reply ingest crashed')
        return 'failed'
    return (
        f'fetched={counts["fetched"]} matched={counts["matched"]} '
        f'orphan_lead={counts["orphan_lead"]} '
        f'unmatched={counts["unmatched"]} errors={counts["errors"]}')


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def classify_and_draft_reply_task(self, reply_id):
    """
    Per-reply — classify + draft an auto-reply. Trust level decides
    whether the draft is queued for approval or auto-promoted to
    'approved'.
    """
    from outreach.classifier import classify_and_draft
    from outreach.models import EmailReply

    reply = EmailReply.objects.filter(pk=reply_id).first()
    if reply is None:
        return 'reply not found'
    try:
        result = classify_and_draft(reply)
    except Exception as exc:  # noqa: BLE001
        logger.exception('classify+draft crashed for reply %s', reply_id)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return 'failed'
    return (
        f'classification={result["classification"]} '
        f'needs_human={result["needs_human"]} '
        f'drafted={result["drafted"]} status={result["status"]}')


# ── Scrape scheduler ───────────────────────────────────────────────────────

@shared_task
def run_scrape_jobs_task():
    """
    Daily at 02:00 — run every active ScrapeJob. Each job feeds its
    discovered leads through outreach.pipeline.import_leads (which
    dedupes + fires enrichment).
    """
    from django.utils import timezone

    from outreach.models import ScrapeJob
    from outreach.pipeline import import_leads
    from outreach.scraper import (
        scrape_georgia_bar_sync,
        scrape_google_maps_sync,
        scrape_texas_bar_sync,
    )

    jobs = ScrapeJob.objects.filter(active=True)
    total_imported = 0
    total_skipped = 0
    for job in jobs:
        err = ''
        imported = skipped = 0
        try:
            if job.source == 'google_maps':
                state_full = 'Texas' if job.state == 'TX' else 'Georgia'
                raw, _ = scrape_google_maps_sync(
                    job.niche, job.city, state_full, job.max_results)
                summary = import_leads(
                    raw, source='google_maps',
                    business_type_override=job.niche.title())
            elif job.source == 'texas_bar':
                raw = scrape_texas_bar_sync(
                    city=job.city, practice_area=job.niche,
                    max_results=job.max_results)
                summary = import_leads(
                    raw, source='state_bar',
                    business_type_override=job.niche.title())
            else:  # georgia_bar
                raw = scrape_georgia_bar_sync(
                    city=job.city, practice_area=job.niche,
                    max_results=job.max_results)
                summary = import_leads(
                    raw, source='state_bar',
                    business_type_override=job.niche.title())
            imported = summary.get('imported', 0)
            # 'duplicates' is the fuzzy-match dedupe count; the closest
            # field to "we saw it and threw it away".
            skipped = summary.get('duplicates', 0)
        except Exception as exc:  # noqa: BLE001
            logger.exception('scrape job %s crashed', job.pk)
            err = str(exc)[:500]

        job.last_run_at = timezone.now()
        job.last_run_imported = imported
        job.last_run_skipped = skipped
        job.last_run_error = err
        job.save(update_fields=[
            'last_run_at', 'last_run_imported',
            'last_run_skipped', 'last_run_error', 'updated_at'])
        total_imported += imported
        total_skipped += skipped

    return (
        f'jobs={jobs.count()} imported={total_imported} '
        f'skipped={total_skipped}')
