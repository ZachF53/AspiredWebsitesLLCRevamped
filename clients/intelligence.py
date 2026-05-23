"""
Website Intelligence Engine — Phase 7 Part 3.

`gather_client_data(client)` collects every metric we have on a client
(uptime, keywords, conversions, scan findings, GBP sync, content
freshness, health) into a single dict.

`run_intelligence_analysis(client)` feeds that dict to Claude and asks
it to surface GENUINE improvement opportunities. Claude returns
strict JSON with an overall assessment plus up to 5 suggestions —
each one priced and tagged with whether it falls inside an active
maintenance plan's scope.

The Celery task in `clients.tasks.run_intelligence_for_client` is
the only caller that should persist results; this module returns
plain dicts and never writes to the DB itself.
"""

import json
import logging
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

# Use the same Sonnet model identifier as `reporting.ai` so a model
# change is a one-line edit there rather than scattered across modules.
from reporting.ai import MODEL_CONTENT

logger = logging.getLogger(__name__)


# ── Data gatherer ──────────────────────────────────────────────────────────

def gather_client_data(client):
    """
    Collect every metric available for `client`. Each lookup is
    defensive: missing tables / no data → sensible default rather
    than a raised exception, so a partial-data client still gets a
    Claude analysis.
    """
    data = {
        'firm_name': client.firm_name,
        'business_type': client.business_type or '',
        'city': client.city or '',
        'state': client.state or '',
        'package': client.package or '',
        'maintenance_active': bool(client.maintenance_active),
        'on_maintenance_plan': bool(client.maintenance_active),
        'live_url': '',
        'months_since_launch': None,
    }

    # ── Project ────────────────────────────────────────────────
    project = client.projects.filter(stage='live').first()
    if project:
        data['live_url'] = project.live_url or ''
        if project.launch_date:
            delta = date.today() - project.launch_date
            data['months_since_launch'] = delta.days // 30

    # ── Uptime ─────────────────────────────────────────────────
    try:
        from reporting.uptime_helpers import (
            get_avg_response_time, get_uptime_percentage,
        )
        data['uptime_30d'] = get_uptime_percentage(client, days=30)
        data['uptime_90d'] = get_uptime_percentage(client, days=90)
        data['avg_response_ms'] = get_avg_response_time(
            client, days=30)
    except Exception:
        logger.exception('uptime lookup failed for %s', client.pk)
        data['uptime_30d'] = None
        data['uptime_90d'] = None
        data['avg_response_ms'] = None

    # ── Keywords ───────────────────────────────────────────────
    try:
        from reporting.models import TrackedKeyword
        keywords = (
            TrackedKeyword.objects
            .filter(client=client, is_active=True)
            .prefetch_related('rank_records')
        )
        keyword_data = []
        for kw in keywords:
            latest = kw.rank_records.first()
            if latest:
                keyword_data.append({
                    'keyword': kw.keyword,
                    'position': latest.position,
                    'impressions': latest.impressions,
                    'clicks': latest.clicks,
                })
            else:
                keyword_data.append({
                    'keyword': kw.keyword,
                    'position': None,
                    'impressions': 0,
                    'clicks': 0,
                })
        data['keywords'] = keyword_data
        data['keywords_on_page_1'] = sum(
            1 for k in keyword_data
            if k['position'] and k['position'] <= 10)
        data['keywords_not_ranking'] = sum(
            1 for k in keyword_data if not k['position'])
    except Exception:
        logger.exception('keyword lookup failed for %s', client.pk)
        data['keywords'] = []
        data['keywords_on_page_1'] = 0
        data['keywords_not_ranking'] = 0

    # ── Conversions (30d) ──────────────────────────────────────
    try:
        from reporting.models import ConversionEvent
        thirty_days_ago = timezone.now() - timedelta(days=30)
        data['form_submissions_30d'] = (
            ConversionEvent.objects
            .filter(client=client, event_type='form_submit',
                    event_timestamp__gte=thirty_days_ago)
            .count()
        )
        data['phone_clicks_30d'] = (
            ConversionEvent.objects
            .filter(client=client, event_type='phone_click',
                    event_timestamp__gte=thirty_days_ago)
            .count()
        )
    except Exception:
        logger.exception('conversion lookup failed for %s', client.pk)
        data['form_submissions_30d'] = 0
        data['phone_clicks_30d'] = 0

    # ── Security scans ─────────────────────────────────────────
    try:
        from reporting.models import VulnerabilityScan
        latest_scan = (
            VulnerabilityScan.objects
            .filter(client=client, status='complete')
            .order_by('-completed_at').first()
        )
        if latest_scan:
            data['scan_date'] = (
                latest_scan.completed_at.date().isoformat()
                if latest_scan.completed_at else None)
            data['scan_critical'] = latest_scan.critical_count
            data['scan_high'] = latest_scan.high_count
            data['scan_medium'] = latest_scan.medium_count
            data['open_findings'] = (
                latest_scan.findings.filter(status='open').count())
        else:
            data['scan_date'] = None
            data['scan_critical'] = 0
            data['scan_high'] = 0
            data['scan_medium'] = 0
            data['open_findings'] = 0
    except Exception:
        logger.exception('scan lookup failed for %s', client.pk)
        data['scan_date'] = None
        data['scan_critical'] = 0
        data['scan_high'] = 0
        data['scan_medium'] = 0
        data['open_findings'] = 0

    # ── GBP sync ───────────────────────────────────────────────
    try:
        from reporting.models import GBPSyncCheck
        data['gbp_mismatches'] = (
            GBPSyncCheck.objects
            .filter(client=client, is_mismatch=True, resolved=False)
            .count()
        )
    except Exception:
        logger.exception('GBP lookup failed for %s', client.pk)
        data['gbp_mismatches'] = 0

    # ── Content freshness ──────────────────────────────────────
    try:
        from reporting.models import ContentFreshnessReport
        latest = (
            ContentFreshnessReport.objects
            .filter(client=client).order_by('-generated_at').first()
        )
        if latest:
            data['pages_needing_update'] = latest.pages_needing_update
            data['pages_analyzed'] = latest.pages_analyzed
        else:
            data['pages_needing_update'] = None
            data['pages_analyzed'] = None
    except Exception:
        logger.exception('freshness lookup failed for %s', client.pk)
        data['pages_needing_update'] = None
        data['pages_analyzed'] = None

    # ── Competitor gaps (Phase 7 Part 5) ───────────────────────
    try:
        from clients.models import CompetitorGapReport
        latest_gap_report = (
            CompetitorGapReport.objects
            .filter(client=client, status='complete')
            .order_by('-report_month').first()
        )
        if latest_gap_report:
            data['competitor_gaps_high'] = (
                latest_gap_report.high_priority_gaps)
            data['competitor_gap_titles'] = [
                g.get('title', '')
                for g in (latest_gap_report.gaps or [])
                if g.get('priority') == 'high'
            ][:3]
        else:
            data['competitor_gaps_high'] = 0
            data['competitor_gap_titles'] = []
    except Exception:
        logger.exception(
            'competitor gap lookup failed for %s', client.pk)
        data['competitor_gaps_high'] = 0
        data['competitor_gap_titles'] = []

    # ── Health score ───────────────────────────────────────────
    try:
        from clients.health import get_latest_health_score
        health = get_latest_health_score(client)
        data['health_score'] = health.score
        data['health_status'] = health.health_status
    except Exception:
        logger.exception('health lookup failed for %s', client.pk)
        data['health_score'] = None
        data['health_status'] = None

    return data


# ── Claude runner ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a website performance analyst for \
Aspired Websites LLC, a web design agency specializing in law firms \
and small businesses.

Your job is to analyze client website data and identify GENUINE \
improvement opportunities.

CRITICAL RULES:
1. Only suggest improvements if the DATA supports them. Do not \
invent problems.
2. If everything looks good — say so and return an empty \
suggestions array.
3. Be specific. Vague suggestions like "improve SEO" are not \
acceptable.
4. Each suggestion must include a realistic one-time implementation \
fee ($150-$2000).
5. Maximum 5 suggestions per analysis.
6. Prioritize by business impact.

Return ONLY valid JSON. No explanation text. No markdown. No code \
fences. Just the JSON.

Response format:
{
  "overall_assessment": "2-3 sentence plain English summary of \
client standing",
  "suggestions": [
    {
      "type": "seo|performance|content|security|conversion|\
keyword|competitor|technical|design|other",
      "title": "Short specific title",
      "description": "Plain English explanation of why this matters \
for their business. 2-3 sentences.",
      "expected_impact": "What improvement they can realistically \
expect. Be specific.",
      "implementation_notes": "What would actually be done to \
implement this.",
      "one_time_fee": 500,
      "maintenance_equivalent": "Whether this would be covered by a \
maintenance plan and which tier",
      "is_in_maintenance_scope": false,
      "data_sources": ["uptime", "keywords", "scan"]
    }
  ]
}"""


def _build_data_summary(data):
    """Render the gathered metrics into the prose block Claude sees."""
    top_keywords = data.get('keywords', [])[:5]
    return (
        f"CLIENT: {data['firm_name']}\n"
        f"Business type: {data['business_type'] or 'unspecified'}\n"
        f"Location: {data['city']}, {data['state']}\n"
        f"Live URL: {data['live_url'] or 'Not set'}\n"
        f"Months since launch: "
        f"{data['months_since_launch'] if data['months_since_launch'] is not None else 'Unknown'}\n"
        f"Maintenance plan: "
        f"{'Active — ' + (data['package'] or 'unknown package') if data['on_maintenance_plan'] else 'None — one-time build client'}\n"
        f"\n"
        f"PERFORMANCE DATA:\n"
        f"Uptime (30 days): {data['uptime_30d']}%\n"
        f"Uptime (90 days): {data['uptime_90d']}%\n"
        f"Avg response time: {data['avg_response_ms']}ms\n"
        f"\n"
        f"CONVERSIONS (last 30 days):\n"
        f"Form submissions: {data['form_submissions_30d']}\n"
        f"Phone clicks: {data['phone_clicks_30d']}\n"
        f"\n"
        f"KEYWORD RANKINGS:\n"
        f"Keywords tracked: {len(data.get('keywords', []))}\n"
        f"Keywords on page 1: {data['keywords_on_page_1']}\n"
        f"Keywords not ranking: {data['keywords_not_ranking']}\n"
        f"Top keywords: {json.dumps(top_keywords)}\n"
        f"\n"
        f"SECURITY:\n"
        f"Last scan: {data['scan_date'] or 'Never scanned'}\n"
        f"Critical findings: {data['scan_critical']}\n"
        f"High findings: {data['scan_high']}\n"
        f"Open findings: {data['open_findings']}\n"
        f"\n"
        f"GBP mismatches: {data['gbp_mismatches']}\n"
        f"Pages needing content update: {data['pages_needing_update']}\n"
        f"\n"
        f"COMPETITOR GAPS:\n"
        f"High priority gaps: {data.get('competitor_gaps_high', 0)}\n"
        f"Top gaps: {data.get('competitor_gap_titles', [])}\n"
        f"\n"
        f"HEALTH SCORE: {data['health_score']}/100 "
        f"({data['health_status']})\n"
    )


def _strip_json_fences(text):
    """Claude sometimes wraps JSON in ```json … ``` despite the prompt."""
    s = (text or '').strip()
    if s.startswith('```'):
        s = s.strip('`').strip()
        if s.lower().startswith('json'):
            s = s[4:].strip()
    return s


def run_intelligence_analysis(client):
    """
    Run Claude on `client`'s data. Returns:

        {
            'overall_assessment': str,
            'suggestions': list[dict],
            'tokens_used': int,
            'data_snapshot': dict,
            'error': str,   # only present on failure
        }

    Never raises — every failure mode returns the dict with an
    `error` key so the caller can persist a `failed` report row
    instead of swallowing the run.
    """
    import requests as http_requests

    data = gather_client_data(client)

    if not settings.ANTHROPIC_API_KEY:
        return {
            'overall_assessment': '',
            'suggestions': [],
            'tokens_used': 0,
            'data_snapshot': data,
            'error': 'ANTHROPIC_API_KEY is not configured.',
        }

    user_message = (
        "Analyze this client's website performance data and identify "
        "improvement opportunities:\n\n"
        + _build_data_summary(data) + "\n"
        "Remember: Only suggest genuine improvements backed by the "
        "data. Return empty suggestions array if everything looks good."
    )

    try:
        response = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': settings.ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': MODEL_CONTENT,
                'max_tokens': 2000,
                'system': _SYSTEM_PROMPT,
                'messages': [
                    {'role': 'user', 'content': user_message},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        ai_text = body['content'][0]['text']
        usage = body.get('usage', {}) or {}
        tokens = (int(usage.get('input_tokens', 0) or 0)
                  + int(usage.get('output_tokens', 0) or 0))
        result = json.loads(_strip_json_fences(ai_text))
        return {
            'overall_assessment': result.get('overall_assessment', ''),
            'suggestions': result.get('suggestions', []) or [],
            'tokens_used': tokens,
            'data_snapshot': data,
        }
    except Exception as exc:  # noqa: BLE001 — every failure is captured
        logger.exception('intelligence analysis failed for %s',
                         client.pk)
        return {
            'overall_assessment': '',
            'suggestions': [],
            'tokens_used': 0,
            'data_snapshot': data,
            'error': str(exc)[:500],
        }


# ── Phase 7 Part 4 — Annual Business Health Report ─────────────────────────

def gather_annual_data(client, year):
    """
    Roll up a full calendar year of activity for `client` into one
    dict, suitable for both the WeasyPrint template context and the
    JSON payload sent to Claude for narrative writing.

    Every lookup is defensive — missing tables / no rows fall back
    to a sensible default so a sparse-data client still gets a
    complete report skeleton.
    """
    from datetime import date

    from django.db.models import Avg

    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)

    data = {
        'year': year,
        'firm_name': client.firm_name,
        'business_type': client.business_type or '',
        'city': client.city or '',
        'state': client.state or '',
        'contact_name': client.contact_name or '',
        'live_url': '',
        'launch_date': None,
        'months_as_client': None,
    }

    # ── Project ────────────────────────────────────────────────
    project = client.projects.filter(stage='live').first()
    if project:
        data['live_url'] = project.live_url or ''
        if project.launch_date:
            data['launch_date'] = project.launch_date.isoformat()
            delta = date.today() - project.launch_date
            data['months_as_client'] = delta.days // 30

    # ── Uptime ─────────────────────────────────────────────────
    try:
        # UptimeRecord + UptimeAlert live in clients, not reporting.
        from clients.models import UptimeAlert, UptimeRecord

        uptime_records = UptimeRecord.objects.filter(
            client=client,
            checked_at__date__gte=year_start,
            checked_at__date__lte=year_end,
        )
        total_checks = uptime_records.count()
        up_checks = uptime_records.filter(is_up=True).count()
        data['uptime_annual_avg'] = (
            round((up_checks / total_checks * 100), 2)
            if total_checks else None
        )
        data['total_checks'] = total_checks

        monthly_uptime = []
        for month in range(1, 13):
            month_records = uptime_records.filter(
                checked_at__month=month)
            month_total = month_records.count()
            month_up = month_records.filter(is_up=True).count()
            monthly_uptime.append({
                'month': date(year, month, 1).strftime('%b'),
                'uptime_pct': (
                    round(month_up / month_total * 100, 1)
                    if month_total else None),
                'checks': month_total,
            })
        data['uptime_by_month'] = monthly_uptime

        data['downtime_incidents'] = UptimeAlert.objects.filter(
            client=client,
            alerted_at__date__gte=year_start,
            alerted_at__date__lte=year_end,
        ).count()

        avg_ms = uptime_records.filter(is_up=True).aggregate(
            avg=Avg('response_time_ms'))['avg']
        data['avg_response_ms'] = round(avg_ms) if avg_ms else None
    except Exception:
        logger.exception('annual uptime lookup failed for %s',
                         client.pk)
        data['uptime_annual_avg'] = None
        data['total_checks'] = 0
        data['uptime_by_month'] = []
        data['downtime_incidents'] = 0
        data['avg_response_ms'] = None

    # ── Conversions ────────────────────────────────────────────
    try:
        from reporting.models import ConversionEvent
        year_conversions = ConversionEvent.objects.filter(
            client=client,
            event_timestamp__date__gte=year_start,
            event_timestamp__date__lte=year_end,
        )
        data['form_submissions_annual'] = year_conversions.filter(
            event_type='form_submit').count()
        data['phone_clicks_annual'] = year_conversions.filter(
            event_type='phone_click').count()
        data['cta_clicks_annual'] = year_conversions.filter(
            event_type='cta_click').count()

        # Month-by-month form submissions for the Page 5 chart.
        monthly_forms = []
        for month in range(1, 13):
            cnt = year_conversions.filter(
                event_type='form_submit',
                event_timestamp__month=month).count()
            monthly_forms.append({
                'month': date(year, month, 1).strftime('%b'),
                'count': cnt,
            })
        data['form_submissions_by_month'] = monthly_forms
    except Exception:
        logger.exception('annual conversion lookup failed for %s',
                         client.pk)
        data['form_submissions_annual'] = 0
        data['phone_clicks_annual'] = 0
        data['cta_clicks_annual'] = 0
        data['form_submissions_by_month'] = []

    # ── Keywords ───────────────────────────────────────────────
    try:
        from reporting.models import TrackedKeyword
        keywords = TrackedKeyword.objects.filter(
            client=client, is_active=True)
        data['keywords_tracked'] = keywords.count()

        page_1_count = 0
        improved_count = 0
        for kw in keywords:
            latest = kw.rank_records.order_by('-checked_at').first()
            earliest_this_year = (
                kw.rank_records
                .filter(checked_at__date__gte=year_start)
                .order_by('checked_at').first()
            )
            if latest and latest.position and latest.position <= 10:
                page_1_count += 1
            if (latest and earliest_this_year
                    and latest.position
                    and earliest_this_year.position
                    and latest.position
                    < earliest_this_year.position):
                improved_count += 1
        data['keywords_on_page_1'] = page_1_count
        data['keyword_improvements'] = improved_count
    except Exception:
        logger.exception('annual keyword lookup failed for %s',
                         client.pk)
        data['keywords_tracked'] = 0
        data['keywords_on_page_1'] = 0
        data['keyword_improvements'] = 0

    # ── Security ───────────────────────────────────────────────
    try:
        from reporting.models import (
            VulnerabilityFinding, VulnerabilityScan,
        )
        year_scans = VulnerabilityScan.objects.filter(
            client=client, status='complete',
            completed_at__date__gte=year_start,
            completed_at__date__lte=year_end,
        ).order_by('completed_at')
        data['scans_run'] = year_scans.count()
        data['critical_findings_found'] = sum(
            s.critical_count for s in year_scans)
        data['high_findings_found'] = sum(
            s.high_count for s in year_scans)
        data['findings_resolved'] = (
            VulnerabilityFinding.objects.filter(
                scan__client=client,
                scan__completed_at__date__gte=year_start,
                scan__completed_at__date__lte=year_end,
                status='resolved',
            ).count()
        )
        data['scan_timeline'] = [
            {
                'date': (s.completed_at.date().isoformat()
                         if s.completed_at else ''),
                'type': s.get_scan_type_display(),
                'findings': s.findings_count,
            }
            for s in year_scans[:20]
        ]
    except Exception:
        logger.exception('annual scan lookup failed for %s',
                         client.pk)
        data['scans_run'] = 0
        data['critical_findings_found'] = 0
        data['high_findings_found'] = 0
        data['findings_resolved'] = 0
        data['scan_timeline'] = []

    # ── Intelligence Engine (Phase 7 Part 3) ───────────────────
    try:
        from clients.models import IntelligenceSuggestion
        year_suggestions = IntelligenceSuggestion.objects.filter(
            client=client,
            generated_at__date__gte=year_start,
            generated_at__date__lte=year_end,
        )
        data['intelligence_suggestions_made'] = (
            year_suggestions.count())
        approved_states = [
            'client_approved', 'in_scope', 'implemented',
            'out_of_scope_offered',
        ]
        approved_qs = year_suggestions.filter(
            status__in=approved_states)
        data['intelligence_suggestions_approved'] = (
            approved_qs.count())
        revenue = sum(
            float(s.one_time_fee or 0)
            for s in year_suggestions.filter(
                status__in=['implemented',
                            'out_of_scope_offered'])
        )
        data['intelligence_revenue_generated'] = round(revenue, 2)
        # Up to 3 top approved/implemented suggestions for Page 8.
        data['top_intelligence_suggestions'] = [
            {'title': s.title, 'status': s.status,
             'fee': float(s.one_time_fee or 0)}
            for s in approved_qs.order_by('-generated_at')[:3]
        ]
    except Exception:
        logger.exception('annual intelligence lookup failed for %s',
                         client.pk)
        data['intelligence_suggestions_made'] = 0
        data['intelligence_suggestions_approved'] = 0
        data['intelligence_revenue_generated'] = 0.0
        data['top_intelligence_suggestions'] = []

    # ── Changelog ──────────────────────────────────────────────
    try:
        from clients.models import SiteChangelogEntry
        year_changelog = SiteChangelogEntry.objects.filter(
            client=client,
            date_of_change__gte=year_start,
            date_of_change__lte=year_end,
            is_client_visible=True,
        )
        data['changelog_entries_count'] = year_changelog.count()
        # Group counts by change_type for the Page 6 summary.
        from collections import Counter
        type_counts = Counter(
            year_changelog.values_list('change_type', flat=True))
        data['changelog_by_type'] = [
            {'type': t, 'count': n}
            for t, n in type_counts.most_common()
        ]
        data['changelog_entries'] = [
            {
                'date': e.date_of_change.isoformat(),
                'type': e.get_change_type_display(),
                'title': e.title,
            }
            for e in year_changelog.order_by('-date_of_change')[:20]
        ]
    except Exception:
        logger.exception('annual changelog lookup failed for %s',
                         client.pk)
        data['changelog_entries_count'] = 0
        data['changelog_by_type'] = []
        data['changelog_entries'] = []

    # ── NPS ────────────────────────────────────────────────────
    try:
        from reporting.models import NPSSurvey
        year_nps = NPSSurvey.objects.filter(
            client=client,
            sent_at__date__gte=year_start,
            sent_at__date__lte=year_end,
            score__isnull=False,
        )
        nps_scores = list(
            year_nps.values_list('score', flat=True))
        data['nps_scores'] = nps_scores
        data['nps_average'] = (
            round(sum(nps_scores) / len(nps_scores), 1)
            if nps_scores else None
        )
    except Exception:
        logger.exception('annual NPS lookup failed for %s', client.pk)
        data['nps_scores'] = []
        data['nps_average'] = None

    # ── Maintenance ────────────────────────────────────────────
    data['maintenance_active'] = bool(client.maintenance_active)
    data['maintenance_plan'] = client.package or ''

    return data


def generate_annual_narrative(client, data):
    """
    Ask Claude to write three narrative sections for the annual
    report. Returns (narrative_dict, tokens_used). Never raises —
    on any failure (no API key, network blip, malformed JSON) we
    return a graceful fallback narrative so the report still ships.
    """
    import json as _json
    import requests as http_requests

    fallback = {
        'executive_summary': (
            f"Thank you for a great year with Aspired Websites LLC."),
        'year_in_review': (
            f"We have been proud to support {client.firm_name}'s "
            f"online presence throughout the year."),
        'looking_ahead': (
            f"We look forward to continuing to grow your online "
            f"presence in the year ahead."),
    }

    if not settings.ANTHROPIC_API_KEY:
        return fallback, 0

    system_prompt = (
        "You are writing an annual business review for a web "
        "design agency client. Your tone is warm, professional, "
        "and focused on business outcomes — not technical "
        "details.\n\n"
        "Write as if you are Zachery Long, the owner of Aspired "
        "Websites LLC, summarizing a year of partnership with "
        "this client.\n\n"
        "Return ONLY valid JSON with these three fields:\n"
        "{\n"
        '  "executive_summary": "2-3 sentences. Overall year in '
        'review. Highlight the biggest wins. Honest about any '
        'challenges.",\n'
        '  "year_in_review": "3-4 paragraphs. Walk through what '
        'happened this year — uptime performance, security work '
        'done, any improvements made, leads generated. Reference '
        'specific numbers from the data. Conversational, not '
        'bullet points.",\n'
        '  "looking_ahead": "1-2 paragraphs. What opportunities '
        'exist for next year. If they are not on a maintenance '
        'plan, this is a natural place to mention how a plan '
        'would help. Keep it genuine — not salesy."\n'
        "}\n\n"
        "Return only the JSON. No markdown, no code fences, no "
        "commentary."
    )

    user_message = (
        "Write the annual report narrative for this client:\n\n"
        + _json.dumps(data, indent=2, default=str) + "\n\n"
        "Remember: warm, professional tone. Reference real "
        "numbers. Focus on business impact, not technical "
        "details."
    )

    try:
        response = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': settings.ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': MODEL_CONTENT,
                'max_tokens': 2000,
                'system': system_prompt,
                'messages': [
                    {'role': 'user', 'content': user_message},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        ai_text = body['content'][0]['text']
        usage = body.get('usage', {}) or {}
        tokens = (int(usage.get('input_tokens', 0) or 0)
                  + int(usage.get('output_tokens', 0) or 0))
        result = _json.loads(_strip_json_fences(ai_text))
        # Make sure all three keys exist so the template never
        # renders an empty <p>{{ undefined }}</p>.
        merged = dict(fallback)
        for k in ('executive_summary', 'year_in_review',
                  'looking_ahead'):
            v = result.get(k)
            if v:
                merged[k] = v
        return merged, tokens
    except Exception as exc:  # noqa: BLE001 — surface to caller
        logger.exception(
            'annual narrative generation failed for %s', client.pk)
        return fallback, 0


# ── Phase 7 Part 5 — Competitor Content Gap Tracker ────────────────────────

_CRAWL_USER_AGENT = (
    'AspiredWebsites-Analyzer/1.0 (website analysis bot)')
_CRAWL_SKIP_EXTS = (
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp',
    '.zip', '.docx', '.xlsx', '.pptx', '.mp4', '.mp3', '.css',
    '.js', '.xml', '.ico',
)


def crawl_site_for_pages(base_url, max_pages=30, timeout=10):
    """
    BFS-crawl a site and return a list of `{url, title, word_count,
    path}` dicts. Same-domain only, skips non-HTML and known binary
    extensions, 500ms politeness delay between fetches.

    Uses `requests` + `parsel` (parsel ships with Scrapy which is
    already in requirements — no new deps). Failures on individual
    pages are swallowed silently; the caller cares about the
    aggregate.
    """
    import time
    from urllib.parse import urljoin, urlparse

    import requests as http_requests
    from parsel import Selector

    if not base_url.startswith(('http://', 'https://')):
        base_url = f'https://{base_url}'
    base_url = base_url.rstrip('/')

    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc
    if not base_domain:
        return []

    visited = set()
    to_visit = [base_url]
    pages = []

    headers = {'User-Agent': _CRAWL_USER_AGENT}

    while to_visit and len(pages) < max_pages:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = http_requests.get(
                url, headers=headers, timeout=timeout,
                allow_redirects=True,
            )
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        content_type = resp.headers.get('content-type', '')
        if 'html' not in content_type.lower():
            continue

        try:
            sel = Selector(text=resp.text)
        except Exception:
            continue

        # Title — title tag first, fall back to first h1, then URL.
        title = (sel.css('title::text').get()
                 or sel.css('h1::text').get()
                 or url)
        title = (title or '').strip()[:200]

        # Strip script/style/nav/footer before word-counting.
        for sink in sel.css(
                'script, style, nav, footer, header, aside'):
            sink.root.getparent().remove(sink.root) \
                if sink.root.getparent() is not None else None
        body_text = ' '.join(t.strip() for t in
                             sel.css('body ::text').getall()
                             if t.strip())
        word_count = len(body_text.split())

        pages.append({
            'url': url,
            'title': title,
            'word_count': word_count,
            'path': urlparse(url).path or '/',
        })

        # Queue internal links — same-domain, not anchors, not assets.
        for link in sel.css('a::attr(href)').getall():
            absolute = urljoin(url, link)
            parsed = urlparse(absolute)
            if parsed.netloc != base_domain:
                continue
            if '#' in absolute:
                absolute = absolute.split('#', 1)[0]
            if not absolute or absolute in visited:
                continue
            lower = absolute.lower()
            if any(lower.endswith(ext) for ext in _CRAWL_SKIP_EXTS):
                continue
            if absolute not in to_visit:
                to_visit.append(absolute)

        time.sleep(0.5)

    return pages


_GAP_SYSTEM_PROMPT = """You are a website content strategist \
analyzing gaps between a client's website and their competitors.

Your job is to find GENUINE content gaps — pages or topics that \
competitors have that the client is missing, which could help \
the client attract more search traffic and leads.

CRITICAL RULES:
1. Only flag REAL gaps — do not invent problems that don't exist \
in the data.
2. Focus on business-relevant content:
   - Practice area pages (for law firms)
   - Service pages (for service businesses)
   - Location pages (city/state targeting)
   - FAQ or resource pages that drive traffic
3. Do not flag minor differences in blog posts or news articles.
4. Maximum 8 gaps. Prioritize by business impact.
5. Be specific — name the exact page or topic that's missing.

Return ONLY valid JSON. No markdown. No code fences. Just JSON.

Response format:
{
  "overall_assessment": "2-3 sentences summarizing the competitive \
content gap situation",
  "gaps": [
    {
      "gap_type": "missing_page",
      "title": "Missing: Personal Injury Attorney page",
      "description": "2 of 3 competitors have a dedicated personal \
injury page. Client only has a general services page.",
      "competitors_with_this": ["Johnson Law", "Smith & Associates"],
      "estimated_search_volume": "high",
      "suggested_action": "Create a dedicated Personal Injury \
Attorney page targeting local keywords",
      "suggested_page_title": "Personal Injury Attorney in San \
Antonio, TX",
      "priority": "high"
    }
  ]
}

gap_type options:
- missing_page: competitor has a page client doesn't
- keyword_gap: competitor targets keywords client ignores
- thin_content: client has the page but it's much shorter
- missing_section: client page exists but missing key info"""


def analyze_competitor_gaps(client, client_pages, competitor_data):
    """
    Hand the crawl results to Claude and parse a strict-JSON gap
    list back. Returns `(result_dict, tokens_used)` and never
    raises — failures yield an empty `gaps` list with an
    apologetic assessment so the caller can still save a
    `complete` (just empty) report.
    """
    import json as _json

    import requests as http_requests

    fallback = (
        {'overall_assessment': 'Analysis could not be completed.',
         'gaps': []},
        0,
    )
    if not settings.ANTHROPIC_API_KEY:
        return fallback

    client_page_titles = [p['title'] for p in client_pages]
    client_paths = [p['path'] for p in client_pages]
    competitor_summaries = []
    for comp in competitor_data:
        competitor_summaries.append({
            'name': comp['competitor_name'],
            'domain': comp['competitor_domain'],
            'page_count': len(comp.get('pages', [])),
            'page_titles': [p['title']
                            for p in comp.get('pages', [])[:20]],
            'paths': [p['path']
                      for p in comp.get('pages', [])[:20]],
        })

    user_message = (
        f"Analyze content gaps for this client vs their "
        f"competitors:\n\n"
        f"CLIENT: {client.firm_name}\n"
        f"Business type: {client.business_type or 'unspecified'}\n"
        f"Location: {client.city or '?'}, {client.state or '?'}\n\n"
        f"CLIENT'S PAGES ({len(client_pages)} total):\n"
        f"Titles: {_json.dumps(client_page_titles[:25])}\n"
        f"Paths: {_json.dumps(client_paths[:25])}\n\n"
        f"COMPETITORS:\n"
        f"{_json.dumps(competitor_summaries, indent=2)}\n\n"
        "Find genuine content gaps where competitors have pages "
        "or topics the client is missing."
    )

    try:
        response = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': settings.ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': MODEL_CONTENT,
                'max_tokens': 2000,
                'system': _GAP_SYSTEM_PROMPT,
                'messages': [
                    {'role': 'user', 'content': user_message},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        ai_text = body['content'][0]['text']
        usage = body.get('usage', {}) or {}
        tokens = (int(usage.get('input_tokens', 0) or 0)
                  + int(usage.get('output_tokens', 0) or 0))
        result = _json.loads(_strip_json_fences(ai_text))
        # Ensure shape.
        if not isinstance(result, dict):
            return fallback
        result.setdefault('overall_assessment', '')
        result.setdefault('gaps', [])
        if not isinstance(result['gaps'], list):
            result['gaps'] = []
        return result, tokens
    except Exception:
        logger.exception(
            'competitor gap analysis failed for %s', client.pk)
        return fallback
