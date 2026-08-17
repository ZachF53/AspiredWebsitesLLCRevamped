"""
Reporting — public endpoints: conversion tracking, NPS survey responses, and
the AI chatbot API. The tracking + chatbot endpoints are CSRF-exempt (external
sites post here), rate limited per IP, and CORS-open.
"""

import hashlib
import json
import re
import uuid
from functools import wraps

from django.conf import settings
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .models import ConversionEvent

VALID_EVENT_TYPES = {'form_submit', 'phone_click', 'cta_click'}


def _website_for_tracker_id(raw_id):
    """Resolve the id in a tracker snippet to a Website, or None.

    The snippet is `<script data-aspired-client="UUID">`, sitting in the
    HTML of a client's live site. We cannot redeploy those, and every one
    already out there carries a ClientProfile id. So both forms resolve:
    a Website id for snippets generated from now on, and a legacy profile
    id for everything already in the wild — indefinitely, because a
    client site can stay untouched for years.

    The legacy branch filters Account on its own `legacy_client_profile`
    column rather than importing ClientProfile, so supporting the old
    snippets does not count as a legacy read.

    A profile id maps to the account's oldest site. That is a guess on a
    multi-site account, but the snippet genuinely does not say which site
    it is on, and dropping the event entirely would be worse: the client
    would see their conversions stop.
    """
    from clients.account_models import Website

    if not raw_id:
        return None
    site = (Website.objects
            .select_related('account')
            .filter(id=raw_id)
            .first())
    if site is not None:
        return site
    return (Website.objects
            .select_related('account')
            .filter(account__legacy_client_profile_id=raw_id)
            .order_by('created_at')
            .first())


# Preflight cached for 24h — browser skips OPTIONS for subsequent POSTs.
_CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
}


def cors_post(view_func):
    """
    Decorator for public tracking endpoints called cross-origin from client
    sites. Handles CORS preflight + method gating in one place.

    - OPTIONS preflight → 200 with CORS headers (so the browser proceeds
      with the real POST instead of aborting on a 405)
    - POST → calls the view, then attaches CORS headers to its response
    - Anything else → 405

    Apply OUTSIDE @csrf_exempt and @ratelimit so the preflight short-circuits
    before rate-limiting (preflights shouldn't count) and before CSRF (OPTIONS
    has no body to check anyway).
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.method == 'OPTIONS':
            response = HttpResponse(status=200)
            for k, v in _CORS_HEADERS.items():
                response[k] = v
            return response

        if request.method != 'POST':
            return HttpResponse(status=405)

        response = view_func(request, *args, **kwargs)
        for k, v in _CORS_HEADERS.items():
            response[k] = v
        return response

    return wrapper


def _ok():
    """A 200 response, CORS-open so cross-origin beacons never error."""
    resp = JsonResponse({'status': 'ok'})
    resp['Access-Control-Allow-Origin'] = '*'
    return resp


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _hash_ip(request):
    """A salted SHA-256 of the visitor IP — for dedup, never stored raw."""
    ip = request.META.get('REMOTE_ADDR', '')
    if not ip:
        return ''
    return hashlib.sha256(
        (ip + settings.SECRET_KEY).encode('utf-8')).hexdigest()


@cors_post
@csrf_exempt
@ratelimit(key='ip', rate='100/m', block=True)
def track_conversion_event(request):
    """Record one conversion event. Always returns 200 — never leaks info."""
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return _ok()
    if not isinstance(data, dict):
        return _ok()

    event_type = data.get('event_type', '')
    if event_type not in VALID_EVENT_TYPES:
        return _ok()

    client_id = data.get('client_id', '')
    if not _is_uuid(client_id):
        return _ok()
    client = _website_for_tracker_id(client_id)
    if client is None:
        return _ok()

    event_ts = parse_datetime(str(data.get('timestamp') or '')) or timezone.now()

    ConversionEvent.objects.create(
        website_new=client,
        event_type=event_type,
        element_id=str(data.get('element_id') or '')[:100],
        element_text=str(data.get('element_text') or '')[:100],
        page_url=str(data.get('page_url') or '')[:200],
        page_title=str(data.get('page_title') or '')[:200],
        event_timestamp=event_ts,
        ip_hash=_hash_ip(request),
    )
    return _ok()


# ── Tier 1 batched-tracker endpoint ──────────────────────────────────────────

@cors_post
@csrf_exempt
@ratelimit(key='ip', rate='60/m', block=True)
def track_batch(request):
    """
    Receives the page-session beacon from the v2 aspired-tracker.js.

    One request per page view, containing every event that happened
    on that page (scroll milestones, clicks, exit intent, form
    submits, plus a `page_summary` event with the totals). Writes a
    single `PageSession` row plus a `ConversionEvent` per
    form/phone/CTA event so the legacy conversion dashboard keeps
    working.

    Always returns 200 — never leaks whether a client_id was valid
    or whether the batch was accepted.
    """
    from .models import PageSession

    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return _ok()
    if not isinstance(data, dict):
        return _ok()

    client_id = data.get('client_id', '')
    session_id = str(data.get('session_id') or '')[:100]
    events = data.get('events') or []
    if not isinstance(events, list):
        return _ok()
    if not _is_uuid(client_id):
        return _ok()

    site = _website_for_tracker_id(client_id)
    if site is None or not events:
        return _ok()
    client = site

    # Pull the page_summary event (always last on the queue but
    # don't depend on position — find by type).
    summary = next(
        (e for e in events
         if isinstance(e, dict)
         and e.get('event_type') == 'page_summary'),
        {},
    )

    # Conversion-event counts.
    form_submits = sum(
        1 for e in events
        if isinstance(e, dict)
        and e.get('event_type') == 'form_submit')
    phone_clicks = sum(
        1 for e in events
        if isinstance(e, dict)
        and e.get('event_type') == 'phone_click')
    cta_clicks = sum(
        1 for e in events
        if isinstance(e, dict)
        and e.get('event_type') == 'cta_click')

    # First event with a URL wins — keeps malformed entries from
    # blowing this up.
    page_url = ''
    page_title = ''
    for e in events:
        if isinstance(e, dict) and e.get('page_url'):
            page_url = str(e.get('page_url') or '')
            page_title = str(e.get('page_title') or '')
            break

    try:
        PageSession.objects.create(
            website_new=site,
            session_id=session_id,
            page_url=page_url[:2000],
            page_title=page_title[:200],
            time_on_page_seconds=summary.get('time_on_page_seconds'),
            max_scroll_depth=summary.get('max_scroll_depth'),
            scroll_milestones_hit=(
                summary.get('scroll_milestones_hit') or []),
            exit_intent_fired=bool(
                summary.get('exit_intent_fired', False)),
            click_heatmap=(summary.get('click_heatmap') or [])[:50],
            form_submits=form_submits,
            phone_clicks=phone_clicks,
            cta_clicks=cta_clicks,
            raw_events=events[:100],
        )
    except Exception:  # noqa: BLE001 — never raise from a public beacon
        return _ok()

    # Also flush conversion events into the existing
    # ConversionEvent table so the legacy dashboard keeps working.
    now = timezone.now()
    ip_hash = _hash_ip(request)
    for e in events:
        if not isinstance(e, dict):
            continue
        etype = e.get('event_type')
        if etype not in VALID_EVENT_TYPES:
            continue
        ev_ts = (parse_datetime(str(e.get('timestamp') or '')) or now)
        try:
            ConversionEvent.objects.create(
                website_new=site,
                event_type=etype,
                element_id=str(e.get('element_id') or '')[:100],
                element_text=str(e.get('element_text') or '')[:100],
                page_url=page_url[:200],
                page_title=page_title[:200],
                event_timestamp=ev_ts,
                ip_hash=ip_hash,
            )
        except Exception:  # noqa: BLE001 — never raise from a beacon
            continue

    return _ok()


# ── Tracker config — server-side Tier 1 vs Tier 2 flag ─────────────────────

# Cached for 5 minutes so every page load from a busy client site
# doesn't slam the DB, but admin toggles still take effect within
# the window. `Cache-Control: public, max-age=300` is set by
# cache_page automatically for the browser side too.
@cache_page(60 * 5)
def tracker_config(request, client_id):
    """
    Public per-client config endpoint hit by aspired-tracker.js to
    learn whether session recording is enabled. Unknown client_id
    returns a safe "Tier 1, no recording" payload so a typoed UUID
    in a snippet never reveals enumeration info.

    CORS-open because the request originates on the client's own
    domain, not aspiredwebsites.com.
    """
    enabled = False
    try:
        site = _website_for_tracker_id(client_id)
        enabled = bool(site and site.session_recording_enabled)
    except (ValueError, TypeError):
        enabled = False

    payload = {
        'tier': 2 if enabled else 1,
        'session_recording': bool(enabled),
        'client_id': str(client_id),
    }
    response = JsonResponse(payload)
    response['Access-Control-Allow-Origin'] = '*'
    response['Cache-Control'] = 'public, max-age=300'
    return response


# ── Tier 2 session-recording endpoint (rrweb) ───────────────────────────────

@cors_post
@csrf_exempt
@ratelimit(key='ip', rate='120/m', block=True)
def track_recording(request):
    """
    Receives rrweb recording chunks from the in-browser recorder.
    Called every ~10s during a session and once on page unload.

    Only clients with `session_recording_enabled=True` get a
    SessionRecording row written — everyone else is silently dropped
    so the beacon never reveals enablement state.

    Always returns 200, never raises.
    """
    import sys as _sys

    from .models import SessionRecording

    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return _ok()
    if not isinstance(data, dict):
        return _ok()

    client_id = data.get('client_id', '')
    session_id = str(data.get('session_id') or '')[:100]
    events = data.get('events') or []
    is_final = bool(data.get('is_final', False))
    if not _is_uuid(client_id) or not session_id or not events:
        return _ok()

    client = _website_for_tracker_id(client_id)
    if client is None or not client.session_recording_enabled:
        return _ok()

    viewport = data.get('viewport') or {}
    try:
        vp_w = int(viewport.get('width') or 0) or None
        vp_h = int(viewport.get('height') or 0) or None
    except (TypeError, ValueError):
        vp_w = vp_h = None

    # Every read path for recordings — the admin recordings list, the
    # website_detail count, and the portal replay/download views — filters
    # on `website_new`. `client` IS the Website here — the tracker id
    # resolves straight to one.
    site = client

    # Visitor device, read off the request's own User-Agent. Doing this
    # server-side means the tracker snippet on the client's site stays
    # unchanged and no extra bytes ride along in the beacon.
    from .useragent import parse_user_agent
    ua_raw = (request.META.get('HTTP_USER_AGENT') or '')[:400]
    ua = parse_user_agent(ua_raw)

    try:
        rec, _created = SessionRecording.objects.get_or_create(
            website_new=site,
            session_id=session_id,
            defaults={
                'page_url': str(data.get('page_url') or '')[:2000],
                'page_title': str(data.get('page_title') or '')[:200],
                'viewport_width': vp_w,
                'viewport_height': vp_h,
                'device_type': ua['device_type'],
                'browser': ua['browser'],
                'os': ua['os'],
                'user_agent': ua_raw,
                'status': 'recording',
            },
        )
    except Exception:  # noqa: BLE001
        return _ok()

    # A session opened before this shipped keeps receiving chunks on the
    # same row — fill the device in on the first chunk that carries a UA.
    if rec.device_type == 'unknown' and ua['device_type'] != 'unknown':
        rec.device_type = ua['device_type']
        rec.browser = ua['browser']
        rec.os = ua['os']
        rec.user_agent = ua_raw

    # Repair an in-flight session that was opened by an older build (or
    # before the account had a Website) — later chunks land on the same
    # row, so this is the only chance to attach it.
    if rec.website_new_id is None and site is not None:
        rec.website_new = site

    chunks = list(rec.recording_chunks or [])
    chunks.append(events)
    rec.recording_chunks = chunks

    # Rough byte-size estimate so the storage report has numbers to
    # work with — sys.getsizeof is the python overhead, but the
    # serialised JSON length is the part we care about.
    try:
        chunk_bytes = len(json.dumps(events).encode('utf-8'))
    except Exception:  # noqa: BLE001
        chunk_bytes = _sys.getsizeof(events)
    rec.estimated_size_kb = (
        (rec.estimated_size_kb or 0) + max(1, chunk_bytes // 1024))

    if is_final:
        rec.status = 'complete'
        # Compute total duration from the first chunk's first event
        # to this final chunk's last event (rrweb stamps each event
        # with a `timestamp` in ms).
        try:
            first_chunk = chunks[0] if chunks else []
            first_ts = (first_chunk[0].get('timestamp')
                        if first_chunk and
                           isinstance(first_chunk[0], dict)
                        else None)
            last_ts = (events[-1].get('timestamp')
                       if events and
                          isinstance(events[-1], dict)
                       else None)
            if first_ts and last_ts and last_ts > first_ts:
                rec.duration_seconds = int(
                    (last_ts - first_ts) // 1000)
        except Exception:  # noqa: BLE001
            pass

    try:
        rec.save()
    except Exception:  # noqa: BLE001
        pass

    return _ok()


# ── NPS survey response ─────────────────────────────────────────────────────

def _nps_band(score):
    """Promoter (9-10) / passive (7-8) / detractor (0-6)."""
    if score is None:
        return ''
    if score >= 9:
        return 'promoter'
    if score >= 7:
        return 'passive'
    return 'detractor'


def _send_review_request(survey, review_url):
    """
    Email a promoter the direct review link.

    Plain text, personal, one link. This goes to a client who has just
    scored us 9 or 10 — the whole job is to make acting on that take one
    tap, while the goodwill is fresh.
    """
    import logging

    from django.core.mail import send_mail
    logging.getLogger(__name__).info(
        'Sending NPS review request to client %s', survey.client_id)

    client = survey.client
    name = (client.contact_name or client.firm_name or 'there').split()[0]
    send_mail(
        subject='Would you mind leaving us a review?',
        message=(
            f'Hi {name},\n\n'
            f'Thanks for the {survey.score}/10 — that genuinely means a '
            f'lot.\n\n'
            f'If you have a spare minute, a short Google review helps other '
            f'business owners find us more than almost anything else we '
            f'do:\n\n'
            f'{review_url}\n\n'
            f'No pressure at all, and thanks either way.\n\n'
            f'— Zachery Long\n'
            f'Aspired Websites LLC\n'
            f'210-896-2536\n'
        ),
        from_email=settings.EMAIL_FROM_CONTACT,
        recipient_list=[client.user.email],
        fail_silently=False,
    )


def _nps_take_action(survey):
    """Run the band-specific follow-up; return the response_action_taken value."""
    band = _nps_band(survey.score)
    if band == 'promoter':
        # CLAUDE.md's onboarding flow (step 10) specifies a SendGrid
        # review request for high scorers. Previously this branch only
        # recorded the string and sent nothing — the ask existed solely
        # as a button on the thank-you page, which a client sees once
        # and often while on a phone.
        #
        # No URL means no ask. Emailing "please review us" without a
        # link is worse than staying quiet, and inventing a link is not
        # an option.
        review_url = getattr(settings, 'GOOGLE_REVIEW_URL', '')
        if not review_url:
            return 'review_url_not_configured'
        try:
            _send_review_request(survey, review_url)
        except Exception:  # noqa: BLE001 — a failed send must not 500
            import logging
            logging.getLogger(__name__).exception(
                'NPS review request failed for %s', survey.client_id)
            return 'review_email_failed'
        return 'review_requested'
    if band == 'detractor':
        from clients.display import owner_label
        from .tasks import send_admin_alert

        label = owner_label(survey)
        send_admin_alert(
            subject=f'Low NPS from {label}: score {survey.score}',
            message=(
                f'NPS score: {survey.score}/10\n'
                f'Client: {label}\n'
                f'Feedback: {survey.feedback or "(none given)"}'
            ),
        )
        return 'needs_you_created'
    return ''


def nps_response(request, token, score):
    """
    NPS landing page at /nps/<token>/<score>/.

    GET records the score and shows a feedback form; POST saves the feedback,
    runs the band-specific action, and shows the thank-you screen.
    """
    from .models import NPSSurvey

    survey = NPSSurvey.objects.filter(survey_token=token).first()
    if survey is None or not 0 <= score <= 10:
        return render(request, 'reporting/nps_landing.html',
                      {'invalid': True}, status=404)

    if survey.score is None:
        survey.score = score
        survey.responded_at = timezone.now()
        survey.save(update_fields=['score', 'responded_at', 'updated_at'])

    band = _nps_band(survey.score)

    if request.method == 'POST':
        survey.feedback = (request.POST.get('feedback') or '').strip()
        survey.response_action_taken = _nps_take_action(survey)
        survey.save(update_fields=[
            'feedback', 'response_action_taken', 'updated_at'])
        return render(request, 'reporting/nps_landing.html', {
            'survey': survey,
            'band': band,
            'submitted': True,
            'google_review_url': getattr(settings, 'GOOGLE_REVIEW_URL', ''),
        })

    return render(request, 'reporting/nps_landing.html', {
        'survey': survey,
        'band': band,
        'submitted': False,
    })


# ── AI chatbot ──────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_PHONE_RE = re.compile(
    r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')


def _build_chat_system_prompt(client, chatbot):
    """Assemble the chatbot system prompt from the site + chatbot config.

    `client` is a Website. The brand the bot speaks as is the site's own
    name: a visitor on the mediation site should not be greeted by the
    law firm.
    """
    from .ai import client_location_phrase
    account = client.account
    biz = client.business_type or 'business'
    return (
        f'You are a helpful assistant for {client.name}, a {biz}'
        f'{client_location_phrase(account)}.\n\n'
        f'{chatbot.system_prompt}\n\n'
        'IMPORTANT RULES:\n'
        '- You are not a lawyer and cannot give legal advice.\n'
        '- Always recommend scheduling a consultation for specific legal '
        'questions.\n'
        '- Be warm, professional, and helpful.\n'
        '- If someone seems to have an urgent legal issue, give them the '
        f"firm's phone number: "
        f"{(account.phone if account else '') or 'our office'}.\n"
        '- If the visitor shares their name or asks to book an appointment, '
        'acknowledge it and offer to have someone follow up.\n'
        '- Keep responses concise — 2-3 short paragraphs maximum.\n'
        '- Never make up facts about cases or outcomes.'
    )


def _detect_lead(conversation, message):
    """Capture an email/phone from a visitor message onto the conversation."""
    email = _EMAIL_RE.search(message)
    phone = _PHONE_RE.search(message)
    if email and not conversation.visitor_email:
        conversation.visitor_email = email.group(0)[:254]
    if phone and not conversation.visitor_phone:
        conversation.visitor_phone = phone.group(0)[:20]
    if (conversation.visitor_email or conversation.visitor_phone) \
            and not conversation.lead_captured:
        conversation.lead_captured = True


def chatbot_config(request, client_id):
    """Public config for the chat widget — greeting, colour, position."""
    from .models import ClientChatbot
    chatbot = (ClientChatbot.objects.filter(client_id=client_id).first()
               if _is_uuid(str(client_id)) else None)
    if chatbot is None or not chatbot.is_active:
        return _cors_json({'active': False})
    return _cors_json({
        'active': True,
        'greeting': chatbot.greeting_message,
        'color': chatbot.primary_color,
        'position': chatbot.position,
    })


@csrf_exempt
@require_POST
@ratelimit(key='ip', rate='20/m', block=True)
def chatbot_api(request):
    """Public chatbot endpoint — POST /api/chat/. Returns a Claude reply."""
    from .ai import MODEL_CHAT, AIError, claude_complete
    from .models import ChatbotConversation, ClientChatbot

    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return _cors_json({'error': 'Bad request'}, status=400)
    if not isinstance(data, dict):
        return _cors_json({'error': 'Bad request'}, status=400)

    client_id = data.get('client_id', '')
    session_id = str(data.get('session_id') or '')[:100]
    message = str(data.get('message') or '').strip()
    history = data.get('conversation_history') or []

    if not _is_uuid(client_id) or not session_id or not message:
        return _cors_json({'error': 'Bad request'}, status=400)

    client = _website_for_tracker_id(client_id)
    chatbot = getattr(client, 'chatbot_new', None) if client else None
    if chatbot is None or not chatbot.is_active:
        return _cors_json({'error': 'Chatbot unavailable'}, status=403)

    conversation, created = ChatbotConversation.objects.get_or_create(
        chatbot=chatbot, session_id=session_id, defaults={'messages': []})
    if created:
        ClientChatbot.objects.filter(pk=chatbot.pk).update(
            total_conversations=F('total_conversations') + 1)

    claude_messages = []
    for item in history[-20:]:
        if not isinstance(item, dict):
            continue
        role, content = item.get('role'), str(item.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            claude_messages.append({'role': role, 'content': content[:4000]})
    claude_messages.append({'role': 'user', 'content': message[:4000]})

    try:
        reply = claude_complete(
            claude_messages,
            system=_build_chat_system_prompt(client, chatbot),
            model=MODEL_CHAT, max_tokens=600,
        )
    except AIError:
        reply = (
            f"Thanks for reaching out! I'm having trouble responding right "
            f"now — please call {client.phone or 'our office'} and we'll be "
            f"glad to help."
        )

    now_iso = timezone.now().isoformat()
    conversation.messages = (conversation.messages or []) + [
        {'role': 'user', 'content': message, 'timestamp': now_iso},
        {'role': 'assistant', 'content': reply, 'timestamp': now_iso},
    ]
    was_lead = conversation.lead_captured
    _detect_lead(conversation, message)
    conversation.save()
    if conversation.lead_captured and not was_lead:
        ClientChatbot.objects.filter(pk=chatbot.pk).update(
            leads_captured=F('leads_captured') + 1)

    return _cors_json({'response': reply, 'session_id': session_id})


def _cors_json(payload, status=200):
    resp = JsonResponse(payload, status=status)
    resp['Access-Control-Allow-Origin'] = '*'
    return resp
