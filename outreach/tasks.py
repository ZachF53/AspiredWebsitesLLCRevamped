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
                    business_type_for_niche,
                    run_lead_search,
                )
                # NOT job.niche.title(). A ScrapeJob for "family law"
                # would run as business_type "Family Law", which no
                # Apollo industry normalises to — the ICP screen would
                # reject every row the run just paid for. Resolved once
                # and reused below so the industry filter, the screen and
                # the push-time segment gate all judge by one type.
                job_type = business_type_for_niche(job.niche)
                try:
                    raw, _ledger = run_lead_search(
                        niche=job.niche, city=job.city, state=job.state,
                        max_results=job.max_results, label=job.name,
                        business_type=job_type)
                except ApifyQuotaReached as exc:
                    logger.info('scrape job %s: %s', job.pk, exc)
                    job.last_run_at = timezone.now()
                    job.last_run_error = str(exc)[:500]
                    job.save(update_fields=[
                        'last_run_at', 'last_run_error', 'updated_at'])
                    continue
                summary = import_leads(
                    raw, source='apify',
                    business_type_override=job_type or None)
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
    from django.conf import settings

    from outreach import verify
    from outreach.models import Lead

    # UNVERIFIED means "passed the free screen, but no vendor was
    # configured when we looked". Once a vendor IS configured those leads
    # need a second pass -- without this they stay permanently unsendable
    # and the funnel silently stops at a stage that already ran.
    statuses = [verify.PENDING]
    if (getattr(settings, 'EMAIL_VERIFY_PROVIDER', '')
            and getattr(settings, 'EMAIL_VERIFY_API_KEY', '')):
        statuses.append(verify.UNVERIFIED)

    leads = Lead.objects.filter(
        email_verification_status__in=statuses,
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

    # Inbound leads are excluded here as well as at push time. An
    # icebreaker costs a Claude call, and cold-email copy for somebody
    # who already wrote to us is money spent on a thing that must never
    # be sent.
    candidates = Lead.objects.filter(
        icebreaker='',
        unsubscribed=False,
        enrichment_completed_at__isnull=False,
    ).exclude(
        source__in=Lead.INBOUND_SOURCES,
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
def run_prospect_task(trigger='scheduled'):
    """Wake the Prospect agent for one cycle.

    Separate from run_outreach_pipeline_task on purpose. The pipeline is
    mechanical and must keep running on its own schedule whether or not
    the agent is healthy, paused, or out of budget — an agent failure
    should never stop leads being verified and enriched.
    """
    from outreach.agent_runtime import run_prospect
    return run_prospect(trigger=trigger)


@shared_task
def run_chat_turn_task(run_id):
    """Answer one chat message.

    Queued rather than run in the request: a turn makes several Claude
    calls and can take a minute, which would hold a gunicorn worker and
    time out behind nginx. The page polls for the answer instead.

    All the real work — and all the failure handling — is in
    ``agent_chat.run_chat_turn``, so the same turn can be executed from a
    shell or a test without a broker.
    """
    from outreach.agent_chat import run_chat_turn
    return run_chat_turn(run_id)


@shared_task
def run_approved_action_task(action_id):
    """Execute an approved COMMIT call, then let Prospect carry on.

    The follow-up turn is what makes "source San Antonio and run the
    pipeline" one instruction instead of two. Prospect sees the real
    result of the scrape it asked for and can immediately call the next
    step, which is free and needs no approval.

    Chained here rather than inside run_approved_action so the execution
    stands on its own: if the follow-up turn fails, the scrape still
    happened and is still recorded.
    """
    from admin_dashboard.models import AIEmployeeAction, AIEmployeeRun
    from outreach.agent_chat import run_approved_action, run_chat_turn

    status = run_approved_action(action_id)

    action = (AIEmployeeAction.objects
              .select_related('run__conversation', 'run__employee')
              .filter(pk=action_id).first())
    conversation = action.run.conversation if action else None
    if conversation is None:
        return status

    follow_up = AIEmployeeRun.objects.create(
        employee=action.run.employee, conversation=conversation,
        trigger='chat')
    run_chat_turn(follow_up.pk)
    return status


@shared_task
def google_profile_backfill_task(limit=50):
    """Copy Google ratings onto qualified leads so openers have a fact.

    Sits between verification and the icebreaker because it only spends
    on leads already known to be contactable, and because the icebreaker
    is what consumes the result.
    """
    from outreach.google_profile import backfill

    summary = backfill(limit=limit)
    if summary.get('reason'):
        return summary['reason']
    return (f"looked_up={summary['looked_up']} matched={summary['matched']} "
            f"citable={summary['citable']} no_listing={summary['no_listing']} "
            f"rejected={summary['rejected']} errors={summary['errors']}")


@shared_task
def assign_campaigns_task(limit=500):
    """Place ready leads into an A/B arm.

    The stage between "personalised" and "pushed" that was missing, and
    whose absence meant push_to_instantly_task selected from an empty set
    forever. See outreach/assignment.py for why the arm is the offer and
    not the city.
    """
    from outreach.assignment import assign_leads

    summary = assign_leads(limit=limit)
    parts = [f"assigned={summary['assigned']}"]
    if summary['by_campaign']:
        parts.append(' '.join(
            f'{name}={n}' for name, n in summary['by_campaign'].items()))
    if summary['skipped_no_campaign']:
        # Named, not just counted. A lead that is ready but belongs to no
        # arm is invisible in every other view -- it is not an error, so
        # nothing logs it, and it is not in a campaign, so no campaign
        # reports it.
        reasons = '; '.join(
            f'{reason} ({n})' if isinstance(n, int) else str(reason)
            for reason, n in summary['reasons'].items())
        parts.append(
            f"unassigned={summary['skipped_no_campaign']} [{reasons}]")
    return ' | '.join(parts)


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

    # Check once up front so a disabled switch reads as one clear line
    # rather than the same refusal repeated per campaign.
    allowed, why = instantly.sending_allowed()
    if not allowed:
        logger.info('push_to_instantly_task: %s', why)
        return f'not sending yet: {why}'

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
    # Verify runs TWICE, on purpose.
    #
    # Sources differ in whether a lead arrives with an email. The Apify
    # person-level database supplies one, so screening before enrichment
    # is what stops a role address costing a PageSpeed fetch and a Claude
    # call. Google Places supplies none -- the address is only discovered
    # DURING enrichment -- so a single pre-enrichment pass would leave
    # every Maps lead permanently unverified and therefore unsendable.
    #
    # Both passes are cheap and idempotent: the second only looks at rows
    # still PENDING or UNVERIFIED, which after the first pass means
    # exactly the ones enrichment just found an address for.
    results = [
        f'verify(pre): {verify_leads_task()}',
        f'enrich: {enrich_pending_leads_task()}',
        f'verify(post): {verify_leads_task()}',
        # The Places join runs AFTER verification and BEFORE the
        # icebreaker: after, so a paid lookup is only ever spent on a
        # lead we know is contactable; before, because the rating it
        # copies across is what the opener has to say.
        f'gprofile: {google_profile_backfill_task()}',
        f'icebreak: {generate_icebreakers_task()}',
        # Assignment sits here and nowhere else: it needs the icebreaker
        # written (an unpersonalised lead is not ready for an arm) and it
        # must happen before push, which selects BY campaign.
        f'assign: {assign_campaigns_task()}',
        f'push: {push_to_instantly_task()}',
    ]
    logger.info('run_outreach_pipeline_task: %s', ' | '.join(results))
    return ' | '.join(results)


@shared_task
def poll_instantly_replies_task(limit=100):
    """Fetch new unibox messages from Instantly and ingest them.

    This is the reply path on plans without outbound webhooks. Webhooks
    are gated behind a higher Instantly tier; GET /emails is not, so
    replies arrive by polling instead. Both paths converge on the same
    ``process_event``, so the sender filter cannot apply to one and not
    the other.

    Beat: every 15 minutes. Nobody expects a cold-email reply answered in
    ninety seconds, and the draft waits for approval regardless.
    """
    from outreach.instantly_poll import poll_replies

    summary = poll_replies(limit=limit)
    if summary.get('error'):
        return f"error: {summary['error']}"
    return (f"polled={summary['polled']} inbound={summary['inbound']} "
            f"new={summary['new']} replies={summary['replies']} "
            f"bounces={summary['bounces']} filtered={summary['filtered']}")


@shared_task
def enrich_pending_leads_task(limit=25):
    """Backfill enrichment for leads that never got it.

    ``enrich_lead_task`` only fires from ``import_leads`` at the moment a
    lead is created. Anything imported before enrichment existed, or
    whose task died, or that arrived while Celery was down, stays
    un-enriched forever -- and an un-enriched lead has no measured
    signals, so ``generate_icebreakers_task`` skips it and it silently
    never reaches a campaign. Nothing in the funnel reported this,
    because the stage had simply never run rather than failed.

    Runs newest-and-highest-scoring first, in small batches: enrichment
    is ~20-30s of HTTP per lead (homepage fetch, TLS probe, PageSpeed,
    up to 3 Brave queries), so a large limit would hold a worker for
    hours and burn the Brave free tier in one pass.
    """
    from outreach.enricher import enrich_lead
    from outreach.models import Lead

    # Newest first, NOT highest-scoring first.
    #
    # Score is derived FROM enrichment -- PageSpeed, TLS, copyright age.
    # Before a lead is enriched its score reflects almost nothing, so
    # ordering the enrichment queue by score inverts the priority it is
    # trying to express: unenriched leads score low, so they enrich last,
    # so they stay scored low.
    #
    # Observed 2026-08-23: a fresh scrape of 95 Texas law firms scored 1
    # each and sat behind 90 older leads scoring 4-8. The batch you just
    # pulled is the batch you want processed, so recency wins.
    # Skip leads verification has already killed for good.
    #
    # ROLE and INVALID are terminal verdicts about an address we already
    # hold -- info@ is not going to stop being info@, and enrichment
    # cannot change either verdict. Spending ~30s and a PageSpeed call on
    # them buys nothing.
    #
    # PENDING is NOT skipped: for a Places-sourced lead it means "no
    # address yet", and enrichment is the stage that finds one. Skipping
    # it would strand exactly the leads that need this most.
    from outreach import verify

    leads = Lead.objects.filter(
        enrichment_completed_at__isnull=True,
        unsubscribed=False,
    ).exclude(
        email_verification_status__in=[verify.ROLE, verify.INVALID],
    ).exclude(website='').order_by('-created_at')[:limit]

    done = failed = 0
    for lead in leads:
        try:
            enrich_lead(lead)
            done += 1
        except Exception:
            logger.exception('enrichment failed for lead %s', lead.pk)
            failed += 1

    remaining = Lead.objects.filter(
        enrichment_completed_at__isnull=True,
        unsubscribed=False,
    ).exclude(website='').count()
    return f'enriched={done} failed={failed} remaining={remaining}'
