"""
Reporting — public endpoints: conversion tracking, NPS survey responses, and
the AI chatbot API. The tracking + chatbot endpoints are CSRF-exempt (external
sites post here), rate limited per IP, and CORS-open.
"""

import hashlib
import json
import re
import uuid

from django.conf import settings
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from clients.models import ClientProfile

from .models import ConversionEvent

VALID_EVENT_TYPES = {'form_submit', 'phone_click', 'cta_click'}


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


@csrf_exempt
@require_POST
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
    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        return _ok()

    event_ts = parse_datetime(str(data.get('timestamp') or '')) or timezone.now()

    ConversionEvent.objects.create(
        client=client,
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

@csrf_exempt
@require_POST
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

    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None or not events:
        return _ok()

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
            client=client,
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
                client=client,
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


# ── Tier 2 session-recording endpoint (rrweb) ───────────────────────────────

@csrf_exempt
@require_POST
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

    client = ClientProfile.objects.filter(
        id=client_id, session_recording_enabled=True).first()
    if client is None:
        return _ok()

    viewport = data.get('viewport') or {}
    try:
        vp_w = int(viewport.get('width') or 0) or None
        vp_h = int(viewport.get('height') or 0) or None
    except (TypeError, ValueError):
        vp_w = vp_h = None

    try:
        rec, _created = SessionRecording.objects.get_or_create(
            client=client,
            session_id=session_id,
            defaults={
                'page_url': str(data.get('page_url') or '')[:2000],
                'page_title': str(data.get('page_title') or '')[:200],
                'viewport_width': vp_w,
                'viewport_height': vp_h,
                'status': 'recording',
            },
        )
    except Exception:  # noqa: BLE001
        return _ok()

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


def _nps_take_action(survey):
    """Run the band-specific follow-up; return the response_action_taken value."""
    band = _nps_band(survey.score)
    if band == 'promoter':
        return 'review_requested'
    if band == 'detractor':
        from .tasks import send_admin_alert
        send_admin_alert(
            subject=(f'Low NPS from {survey.client.firm_name}: '
                     f'score {survey.score}'),
            message=(
                f'NPS score: {survey.score}/10\n'
                f'Client: {survey.client.firm_name}\n'
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
    """Assemble the chatbot system prompt from the client + chatbot config."""
    from .ai import client_location_phrase
    biz = client.business_type or 'business'
    return (
        f'You are a helpful assistant for {client.firm_name}, a {biz}'
        f'{client_location_phrase(client)}.\n\n'
        f'{chatbot.system_prompt}\n\n'
        'IMPORTANT RULES:\n'
        '- You are not a lawyer and cannot give legal advice.\n'
        '- Always recommend scheduling a consultation for specific legal '
        'questions.\n'
        '- Be warm, professional, and helpful.\n'
        '- If someone seems to have an urgent legal issue, give them the '
        f"firm's phone number: {client.phone or 'our office'}.\n"
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

    client = ClientProfile.objects.filter(id=client_id).first()
    chatbot = getattr(client, 'chatbot', None) if client else None
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
