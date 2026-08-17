"""
Phase 5c — AI content generation for social posts.

Single public function:
    generate_post_draft(client, platform, prompt, tone=None) -> str

Routes through reporting.ai.claude_complete (so model config + key
loading + AINotConfigured propagation stays single-source).

Platform-specific shaping:
    facebook   max 2200 chars, conversational, warm
    instagram  max 2200 chars, hashtag-friendly, visual-first
    linkedin   max 1300 chars before truncation, professional, value-first
    twitter    max 280  chars, punchy
    other      max 1500 chars, generic
    gbp        intentionally absent — GBP local-posts are generated in
               reporting/ai.py, not here.

Tones (from SocialContentSettings or override):
    professional, friendly, urgent, educational, salesy

Each call injects:
  - client_location_phrase(client)  — keeps Texas/Georgia language consistent
  - account.name                    — for first-person voice
  - account.business context        — keeps law-firm phrasings out of
                                      Moonieful-referred orgs
"""

import logging

from reporting.ai import (
    AINotConfigured,
    MODEL_CONTENT,
    claude_complete,
    client_location_phrase,
)

logger = logging.getLogger(__name__)


PLATFORM_LIMITS = {
    'facebook':  2200,
    'instagram': 2200,
    'linkedin':  1300,
    'twitter':   280,
    'tiktok':    1500,
    'youtube':   1500,
    'pinterest': 500,
    'threads':   500,
    'other':     1500,
}


PLATFORM_GUIDANCE = {
    'facebook': (
        'Write for a Facebook business page. Conversational tone, no '
        'hashtag spam (1–3 max). Open with a hook. End with a clear '
        'next step (visit the site, call us, comment below). '
        'Single paragraph or 2–3 short ones.'
    ),
    'instagram': (
        'Write an Instagram caption. Visual-first — assume the image '
        'carries the moment. Hook in the first line (it\'s all anyone '
        'sees in feed). 5–10 relevant hashtags at the end. Emojis OK '
        'in moderation. Single voice, conversational.'
    ),
    'linkedin': (
        'Write for LinkedIn. Professional but human. Lead with a '
        'concrete insight or story — not a sales pitch. 2–3 short '
        'paragraphs. End with a question that invites reply, or a '
        'soft CTA. No hashtag stuffing — 2–3 maximum.'
    ),
    'twitter': (
        'Write a single tweet. Punchy. Under 280 characters. One idea, '
        'no list, no thread. No hashtags unless absolutely on-topic.'
    ),
    'tiktok': (
        'Write a TikTok caption to accompany a short video. Hook-first, '
        'one or two sentences. 3–5 relevant hashtags.'
    ),
    'youtube': (
        'Write a YouTube video description. Open with what the video is '
        'about. Add 2–3 lines of context. Include relevant timestamps '
        'placeholder and a CTA to subscribe.'
    ),
    'pinterest': (
        'Write a Pinterest pin description. Keyword-rich, helpful. Avoid '
        'salesy language — Pinterest punishes it.'
    ),
    'threads': (
        'Write for Threads. Conversational, low-key, warm. No hashtag '
        'spam. Imagine you\'re texting a friend.'
    ),
    'other': (
        'Write a short, polished social post. Conversational tone, '
        'no jargon.'
    ),
}


TONE_GUIDANCE = {
    'professional': 'Tone: professional, clear, confident.',
    'friendly':     'Tone: warm, conversational, human.',
    'urgent':       'Tone: action-oriented, time-sensitive.',
    'educational':  'Tone: teach-first. Explain a concept clearly.',
    'salesy':       'Tone: direct sales — name the value, push the CTA.',
}


def _platform_guidance(platform):
    return PLATFORM_GUIDANCE.get(platform, PLATFORM_GUIDANCE['other'])


def _tone_guidance(tone):
    return TONE_GUIDANCE.get((tone or '').lower(), '')


def _business_descriptor(owner):
    """Tight one-line description used by the model.

    `owner` is an Account. The business type is per site, so it is read
    from the account's first website when it has one -- a social plan is
    sold to the business, not to one of its sites, and the account itself
    carries no type. Handles the Moonieful-referred case, where the type
    is deliberately blank.
    """
    name = (getattr(owner, 'name', '') or '').strip()
    btype = ''
    try:
        site = owner.websites.order_by('created_at').first()
        btype = (getattr(site, 'business_type', '') or '').strip()
    except Exception:  # noqa: BLE001 -- no account, or no websites yet
        btype = ''
    if name and btype:
        return f'{name}, a {btype.replace("_", " ")}'
    if name:
        return name
    return 'a small business'


def generate_post_draft(account, platform, prompt, tone='friendly'):
    """Generate a social post body for `account` on `platform`.

    Args:
        account   Account the social plan belongs to
        platform  one of SocialChannel.PLATFORM_CHOICES values
        prompt    operator-supplied topic ("Promote our new family-law
                  practice page", "Holiday hours announcement", etc.)
        tone      one of TONE_GUIDANCE keys; defaults to friendly

    Returns:
        str — body text, hard-capped at the platform limit.

    Raises:
        AINotConfigured — propagated from reporting.ai.claude_complete
                          when ANTHROPIC_API_KEY is unset. Caller should
                          surface a clean error and skip the row.
    """
    limit = PLATFORM_LIMITS.get(platform, PLATFORM_LIMITS['other'])
    descriptor = _business_descriptor(account)
    # client_location_phrase already returns " based in City, State".
    # Wrapping it in another " based in {...}" produced
    # "based in  based in San Antonio, TX" in every system prompt.
    where = client_location_phrase(account) or ''

    system = (
        f'You are a social media writer for {descriptor}{where}. '
        f'Voice: first-person plural ("we"). Do not invent facts about '
        f'the business that you were not told. Do not include URLs '
        f'unless asked. Do not use ALL-CAPS or excessive punctuation. '
        f'{_tone_guidance(tone)} {_platform_guidance(platform)} '
        f'Output ONLY the post body — no preamble, no commentary, '
        f'no "Here is a..." prefix.'
    )

    user_message = (
        f'Topic / direction: {prompt.strip()}\n\n'
        f'Write the post now. Keep it under {limit} characters.'
    )

    try:
        body = claude_complete(
            messages=[{'role': 'user', 'content': user_message}],
            system=system,
            model=MODEL_CONTENT,
            max_tokens=900,
        )
    except AINotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'social.ai.generate_post_draft failed for account=%s '
            'platform=%s', getattr(account, 'pk', '?'), platform)
        raise RuntimeError(f'AI generation failed: {exc}') from exc

    body = (body or '').strip()
    if len(body) > limit:
        body = body[:limit].rstrip()
    return body
