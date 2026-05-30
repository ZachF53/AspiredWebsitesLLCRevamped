"""
Thin wrapper around the Anthropic Claude API.

Every call degrades gracefully: if ANTHROPIC_API_KEY is unset the call raises
AINotConfigured; any other failure raises AIError. Callers catch these and
show a friendly message instead of returning a 500.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Sonnet for long-form content; Haiku for fast, cheap chat turns.
MODEL_CONTENT = 'claude-sonnet-4-6'
MODEL_CHAT = 'claude-haiku-4-5-20251001'


class AIError(Exception):
    """Any failure calling Claude."""


class AINotConfigured(AIError):
    """ANTHROPIC_API_KEY is not set."""


def is_configured():
    """True when the Anthropic API key is available."""
    return bool(settings.ANTHROPIC_API_KEY)


def client_location_phrase(client):
    """
    A ' based in City, State' phrase for AI prompts, or '' when the client
    has no location set. Used by the blog and chatbot system prompts.
    """
    parts = [p for p in (client.city, client.state) if p]
    return f' based in {", ".join(parts)}' if parts else ''


def claude_complete(messages, system='', model=MODEL_CHAT, max_tokens=1024):
    """
    Call Claude and return the text of the first content block.

    `messages` is a list of {'role': ..., 'content': ...} dicts.

    Records token usage to ``reporting.models.ClaudeUsage`` after a
    successful response so the AI Usage widget on the admin dashboard
    has live cost data. Recording is best-effort — a DB hiccup never
    masks the returned text.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise AINotConfigured('ANTHROPIC_API_KEY is not set.')
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        kwargs = {'model': model, 'max_tokens': max_tokens, 'messages': messages}
        if system:
            kwargs['system'] = system
        response = client.messages.create(**kwargs)
        # Token accounting — usage shape is {input_tokens, output_tokens}.
        try:
            from reporting.models import ClaudeUsage
            u = getattr(response, 'usage', None)
            if u is not None:
                ClaudeUsage.record(
                    model=model,
                    input_tokens=getattr(u, 'input_tokens', 0),
                    output_tokens=getattr(u, 'output_tokens', 0),
                )
        except Exception:
            logger.exception('claude_complete: ClaudeUsage.record failed')
        return response.content[0].text.strip()
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface every API failure uniformly
        logger.exception('Claude API call failed')
        raise AIError(str(exc)) from exc
