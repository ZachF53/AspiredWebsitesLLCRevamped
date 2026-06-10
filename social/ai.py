"""
Phase 5a — AI post draft generator.

Single public function:

    generate_post_draft(client, prompt) -> str
        Generate a GBP-suitable post body (≤1500 chars) from a topic
        prompt, anchored on the client's location for local SEO
        relevance. Wraps reporting.ai.claude_complete + MODEL_CONTENT.
        Degrades cleanly with AINotConfigured if ANTHROPIC_API_KEY
        is unset (caller surfaces a "Set the key" notice).

5b/5c will add per-platform tone variation + SocialContentSettings.tone
support. For 5a we keep it intentionally simple — single platform,
single voice.
"""

import logging

from reporting.ai import (
    AIError,
    AINotConfigured,
    MODEL_CONTENT,
    claude_complete,
    client_location_phrase,
)

logger = logging.getLogger(__name__)


# GBP local post body cap — matches social.google_gbp.GBP_POST_MAX_CHARS.
GBP_POST_MAX_CHARS = 1500


# System prompt deliberately short. Claude does better with concrete
# direction than long prose. The location phrase is INTERPOLATED, not
# concatenated post-hoc, so it appears at the top of the model's
# context window where it matters most.
_SYSTEM_TEMPLATE = (
    'You write short Google Business Profile posts for a small '
    'business{location}.\n'
    '\n'
    'Rules:\n'
    '- Plain text. No emoji. No hashtags (GBP de-emphasises both).\n'
    '- 250–800 characters is the sweet spot. NEVER exceed '
    '{max_chars} characters total.\n'
    '- One concrete call-to-action at the end (book a call, request '
    'a quote, visit website).\n'
    '- Local references are good when relevant; never fabricate '
    'streets, neighbourhoods, or events.\n'
    '- Match the voice of a professional service business: warm but '
    'concrete, never salesy, never AI-generic.\n'
)


def generate_post_draft(client, prompt):
    """Generate a draft post body for a client.

    Args:
        client:  ClientProfile — used for location-phrase context.
        prompt:  Operator's topic / framing (e.g. "estate planning
                 for blended families").

    Returns:
        A string body, trimmed to ≤ GBP_POST_MAX_CHARS.

    Raises:
        AINotConfigured  ANTHROPIC_API_KEY unset.
        AIError          Other API failure.
    """
    location = client_location_phrase(client) if client else ''
    system = _SYSTEM_TEMPLATE.format(
        location=location, max_chars=GBP_POST_MAX_CHARS)

    user_msg = (
        prompt.strip() if prompt else
        'Write a short post sharing one useful tip for our customers.')
    messages = [{'role': 'user', 'content': user_msg}]

    text = claude_complete(
        messages=messages,
        system=system,
        model=MODEL_CONTENT,
        # GBP cap is 1500 chars ≈ 380 tokens. Set max_tokens to 600
        # to give Claude a little headroom for backup phrasing without
        # routinely producing over-length output.
        max_tokens=600,
    )

    text = (text or '').strip()
    if len(text) > GBP_POST_MAX_CHARS:
        # Hard truncate. A well-behaved model rarely overshoots given
        # the system prompt above, but the publish path also rejects
        # over-length so this is belt-and-suspenders.
        text = text[:GBP_POST_MAX_CHARS].rstrip()
    return text
