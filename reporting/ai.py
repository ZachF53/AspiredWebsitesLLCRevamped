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


def claude_complete(messages, system='', model=MODEL_CHAT, max_tokens=1024):
    """
    Call Claude and return the text of the first content block.

    `messages` is a list of {'role': ..., 'content': ...} dicts.
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
        return response.content[0].text.strip()
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface every API failure uniformly
        logger.exception('Claude API call failed')
        raise AIError(str(exc)) from exc
