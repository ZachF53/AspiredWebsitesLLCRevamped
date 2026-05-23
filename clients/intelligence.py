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
