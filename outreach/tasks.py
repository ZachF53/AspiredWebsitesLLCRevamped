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
        f'skipped_ai={counts["skipped_ai"]} '
        f'rejected_copy={counts.get("rejected_copy", 0)} '
        f'skipped_no_variant={counts.get("skipped_no_variant", 0)}'
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
        f'permanent_failure={counts.get("permanent_failure", 0)} '
        f'suppressed={counts["suppressed"]} '
        f'blocked={counts.get("blocked", 0)}')


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
            if job.source == 'apify':
                # Contacts WITH emails, unlike every other source here.
                # Quota/budget refusals are not failures — they mean the
                # day's sourcing is done, so record and move on quietly.
                from outreach.apify_source import (
                    ApifyQuotaReached,
                    run_lead_search,
                )
                try:
                    raw, _ledger = run_lead_search(
                        niche=job.niche, city=job.city, state=job.state,
                        max_results=job.max_results, label=job.name)
                except ApifyQuotaReached as exc:
                    logger.info('scrape job %s: %s', job.pk, exc)
                    job.last_run_at = timezone.now()
                    job.last_run_error = str(exc)[:500]
                    job.save(update_fields=[
                        'last_run_at', 'last_run_error', 'updated_at'])
                    continue
                summary = import_leads(
                    raw, source='apify',
                    business_type_override=job.niche.title())
            elif job.source == 'google_maps':
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


# ── Instantly pipeline (verify → icebreak → push) ──────────────────────
#
# ORDER MATTERS AND IS NOT ARBITRARY.
#
# Verification runs before icebreaker generation because an icebreaker
# costs a Claude call and a role address is worth zero of them. Under the
# old ordering the expensive step ran on every lead including the 135
# that should never have been contacted.
#
# Each stage is a separate task rather than one long function so a
# failure in one does not roll back the stage before it, and so any
# stage can be re-run alone from the admin.


@shared_task
def verify_leads_task(limit=500):
    """Verify unverified leads. Cheap, safe to run often.

    Role-address suppression needs no vendor and runs regardless of
    whether EMAIL_VERIFY_PROVIDER is configured, so this task does real
    work on a server with no verification key at all.
    """
    from outreach import verify
    from outreach.models import Lead

    leads = Lead.objects.filter(
        email_verification_status=verify.PENDING,
    ).exclude(email='').order_by('-score')[:limit]

    counts = {}
    for lead in leads:
        status = verify.verify_lead(lead)
        counts[status] = counts.get(status, 0) + 1

    logger.info('verify_leads_task: %s', counts)
    return ' '.join(f'{k}={v}' for k, v in sorted(counts.items())) or 'nothing to verify'


@shared_task
def generate_icebreakers_task(limit=50):
    """Write one personalised opening line per sendable lead.

    Only touches leads that passed verification AND finished enrichment —
    an icebreaker written before PageSpeed and SSL are known has nothing
    specific to say, which is the whole point of the line.
    """
    from outreach import icebreaker, verify
    from outreach.models import Lead

    candidates = Lead.objects.filter(
        icebreaker='',
        unsubscribed=False,
        enrichment_completed_at__isnull=False,
    ).exclude(email='').order_by('-score')[:limit * 3]

    written = skipped = failed = 0
    for lead in candidates:
        if written >= limit:
            break
        if not verify.is_sendable(lead.email_verification_status):
            skipped += 1
            continue
        try:
            icebreaker.generate(lead)
            written += 1
        except icebreaker.IcebreakerError as exc:
            logger.warning('icebreaker rejected for lead %s: %s', lead.pk, exc)
            failed += 1
        except Exception:
            logger.exception('icebreaker crashed for lead %s', lead.pk)
            failed += 1

    return f'written={written} skipped={skipped} failed={failed}'


@shared_task
def push_to_instantly_task():
    """Push ready leads into their campaign.

    A lead is ready when it is verified sendable, has an icebreaker, is
    not suppressed, and has not been pushed already. ``push_leads``
    re-checks every one of those rather than trusting this query — it is
    the last gate before a real send.
    """
    from outreach import instantly
    from outreach.models import Lead, OutreachCampaign

    campaigns = OutreachCampaign.objects.filter(
        active=True).exclude(instantly_campaign_id='')
    if not campaigns:
        return 'no active campaigns with an Instantly id'

    lines = []
    for campaign in campaigns:
        leads = Lead.objects.filter(
            campaign=campaign,
            instantly_lead_id='',
            unsubscribed=False,
        ).exclude(icebreaker='').exclude(email='').order_by('-score')

        if not leads.exists():
            lines.append(f'{campaign.name}: nothing ready')
            continue
        try:
            summary = instantly.push_leads(list(leads), campaign)
            lines.append(
                f"{campaign.name}: pushed={summary['pushed']} "
                f"unsendable={summary['skipped_unsendable']} "
                f"errors={summary['errors']}")
        except instantly.InstantlyError as exc:
            logger.exception('push failed for campaign %s', campaign.pk)
            campaign.last_push_error = str(exc)[:500]
            campaign.save(update_fields=['last_push_error', 'updated_at'])
            lines.append(f'{campaign.name}: FAILED — {exc}')

    return ' | '.join(lines)


@shared_task
def run_outreach_pipeline_task():
    """The whole chain, in order. One beat entry instead of four.

    Sourcing is deliberately NOT included: it costs money per run and is
    driven by ScrapeJob on its own schedule, so a pipeline retry can
    never re-trigger a paid scrape.
    """
    results = [
        f'verify: {verify_leads_task()}',
        f'icebreak: {generate_icebreakers_task()}',
        f'push: {push_to_instantly_task()}',
    ]
    logger.info('run_outreach_pipeline_task: %s', ' | '.join(results))
    return ' | '.join(results)
