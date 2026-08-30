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
#
# ⚠️  Changing MODEL_CONTENT/MODEL_CHAT requires a matching rate entry in
# reporting.models.CLAUDE_PRICING_USD_PER_MTOK in the SAME commit —
# ``ClaudeUsage.cost_usd`` returns 0.0 for an unpriced model, which would
# silently zero the AI-usage widget and the outreach spend cap.
#
# Sonnet 5 behaviour change vs 4.6: adaptive thinking is ON when the
# ``thinking`` parameter is omitted (4.6 defaulted to off), and max_tokens
# caps thinking + visible output TOGETHER. Callers that parse structured
# output on a tight budget must pass thinking=THINKING_OFF; callers doing
# open-ended generation should leave it adaptive and size max_tokens with
# headroom. See _split_subject_body in outreach/sender.py for what a
# truncated generation costs us.
MODEL_CONTENT = 'claude-sonnet-5'
MODEL_CHAT = 'claude-haiku-4-5-20251001'

# Convenience constant for callers that need deterministic, immediate
# output (JSON extraction, single-field classification) and would rather
# spend their whole token budget on the answer than on reasoning.
THINKING_OFF = {'type': 'disabled'}


class AIError(Exception):
    """Any failure calling Claude."""


class AINotConfigured(AIError):
    """ANTHROPIC_API_KEY is not set."""


def is_configured():
    """True when the Anthropic API key is available."""
    return bool(settings.ANTHROPIC_API_KEY)


def client_location_phrase(owner):
    """
    A ' based in City, State' phrase for AI prompts, or '' when there is no
    location set. Used by the blog and chatbot system prompts.

    `owner` is an Account (city/state are account-level facts about the
    business). Tolerates None so a site with no account still generates a
    prompt rather than raising mid-request.
    """
    if owner is None:
        return ''
    parts = [p for p in (getattr(owner, 'city', ''),
                         getattr(owner, 'state', '')) if p]
    return f' based in {", ".join(parts)}' if parts else ''


def _first_text_block(response):
    """Text of the first ``text`` content block, stripped.

    This used to be ``response.content[0].text``. That assumed the first
    block is always text — true on Sonnet 4.6, FALSE on Sonnet 5, where
    adaptive thinking is on by default and the response comes back as
    ``[ThinkingBlock, TextBlock]``. A ThinkingBlock has ``.thinking``,
    not ``.text``, so index-0 access raised AttributeError and every
    MODEL_CONTENT call surfaced as a generic AIError.

    Returns '' when the model emitted no text at all (e.g. the whole
    budget went to thinking); callers already treat empty output as a
    failure to handle rather than something to send.
    """
    for block in getattr(response, 'content', None) or []:
        if getattr(block, 'type', '') == 'text':
            return (getattr(block, 'text', '') or '').strip()
    logger.warning(
        'Claude response contained no text block (stop_reason=%s) — the '
        'token budget was likely consumed by thinking.',
        getattr(response, 'stop_reason', '?'))
    return ''


def claude_complete(messages, system='', model=MODEL_CHAT, max_tokens=1024,
                    thinking=None):
    """
    Call Claude and return the text of the first content block.

    `messages` is a list of {'role': ..., 'content': ...} dicts.

    `thinking` is passed straight through to the API when set. Pass
    ``THINKING_OFF`` on structured-output calls where reasoning tokens
    would eat the budget the answer needs; leave it None to accept the
    model's default (adaptive on Sonnet 5, off on Haiku 4.5).

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
        if thinking is not None:
            kwargs['thinking'] = thinking
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
        return _first_text_block(response)
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface every API failure uniformly
        logger.exception('Claude API call failed')
        raise AIError(str(exc)) from exc


def claude_tools(messages, tools, system='', model=MODEL_CHAT,
                 max_tokens=1024, thinking=None):
    """Phase 4.1 — tool-calling variant of claude_complete.

    Args:
      messages: list of {'role','content'} dicts.
      tools:    list of Anthropic tool definitions
                (see https://docs.anthropic.com/en/docs/build-with-claude/tool-use)
                — each {'name', 'description', 'input_schema'}.
      system:   system prompt (optional).

    Returns:
      A dict with one of these shapes:
        {'kind': 'tool_use', 'name': str, 'input': dict}
        {'kind': 'text',     'text': str}

    Raises AINotConfigured if ANTHROPIC_API_KEY is missing,
    AIError on any other API failure — same contract as
    claude_complete.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise AINotConfigured('ANTHROPIC_API_KEY is not set.')
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        kwargs = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': messages,
            'tools': tools,
        }
        if system:
            kwargs['system'] = system
        if thinking is not None:
            kwargs['thinking'] = thinking
        response = client.messages.create(**kwargs)
        # Token accounting — same path as claude_complete.
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
            logger.exception('claude_tools: ClaudeUsage.record failed')

        # Walk the content blocks looking for a tool_use. If we find one,
        # return its name + input. Otherwise return the first text block
        # so the caller can render a clarifying question.
        for block in response.content:
            btype = getattr(block, 'type', '')
            if btype == 'tool_use':
                return {
                    'kind': 'tool_use',
                    'name': getattr(block, 'name', ''),
                    'input': getattr(block, 'input', {}) or {},
                }
        text_parts = []
        for block in response.content:
            if getattr(block, 'type', '') == 'text':
                text_parts.append(
                    getattr(block, 'text', '').strip())
        return {'kind': 'text', 'text': ' '.join(text_parts).strip()}
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception('Claude tool-use API call failed')
        raise AIError(str(exc)) from exc


# ── Multi-turn agent loop (COLD_OUTREACH_AGENT.md §5.1) ────────────────
#
# claude_complete and claude_tools above are SINGLE-TURN: one message in,
# one answer (or one tool pick) out. That is exactly right for "move
# Johnson Law to design" and the admin assistant should keep using it.
#
# It is the wrong shape for "research this lead, decide an angle, draft,
# check the guardrails, decide send-or-hold" — that needs several tool
# calls in sequence, each informed by the last. This is that loop, and it
# is what Prospect (the cold outreach agent) runs on.

# Non-streaming requests much above ~16K max_tokens risk the SDK's HTTP
# timeout, so that is where we switch to streaming. Below it, a plain
# create() keeps the code simpler.
_STREAMING_THRESHOLD_TOKENS = 16_000

# Sonnet 5 shares max_tokens between adaptive thinking, tool_use blocks,
# and visible text. 2048 (the figure in the original brief) leaves an
# agent turn with almost nothing after reasoning; 8K is a working floor.
_DEFAULT_AGENT_MAX_TOKENS = 8_000


def claude_agent_loop(system, tools, tool_executor, user_message,
                      model=MODEL_CONTENT, max_steps=10,
                      max_tokens=_DEFAULT_AGENT_MAX_TOKENS,
                      effort=None, on_usage=None, prior_messages=None,
                      on_text=None):
    """Run a full tool-use loop rather than a single call.

    Args:
      system:        system prompt.
      tools:         Anthropic tool definitions
                     ({'name', 'description', 'input_schema'}).
      tool_executor: callable ``(name: str, input: dict) -> dict | str``,
                     invoked for every tool_use block Claude produces.
                     MUST NOT raise past its own boundary — catch inside
                     and return an error description string instead, so
                     Claude can adapt (try a different lead, ask for a
                     human) rather than the whole run dying on one bad
                     tool call. We defensively catch anyway.
      user_message:  the kick-off instruction.
      effort:        'low'|'medium'|'high'|'xhigh'|'max', or None for the
                     API default. This is the main cost lever — it is a
                     per-AIEmployee setting, deliberately defaulted below
                     the API's own 'high'.
      on_usage:      optional callable ``(model, input_tokens,
                     output_tokens) -> None`` fired after EVERY API call.
                     This is how the daily spend cap stays honest: an
                     8-call run reports 8 times, incrementally, so a run
                     that crashes halfway still counted what it spent.
      prior_messages: earlier turns of an ongoing conversation, in the
                     same wire shape this function returns as
                     ``messages``. ``user_message`` is appended after
                     them. This is what makes a chat pane possible: the
                     model sees what was already said, including the
                     tool_use / tool_result pairs, rather than being
                     handed a lossy prose summary of it. Omit for a
                     one-shot run.

                     Passed through verbatim on purpose — thinking-block
                     signatures and tool_use ids must survive intact or
                     the API rejects the turn.
      on_text:       optional callable ``(chunk: str) -> None`` fired as
                     visible text arrives, so a caller can show the reply
                     being written instead of a spinner. Supplying it
                     switches the call to streaming. Text only — thinking
                     is not emitted. Exceptions raised by it are logged
                     and swallowed.

    Returns:
      {'transcript': [...], 'final_text': str,
       'stopped_reason': 'done' | 'max_steps' | 'error',
       'steps_used': int}

    Stops when Claude produces a turn with no tool_use blocks (it has
    decided it is finished), or at max_steps — the safety valve returns
    what happened so far rather than raising, because a run that hit the
    ceiling still did real work worth logging.

    Records ClaudeUsage per API call, same as claude_tools, so cost stays
    visible per step rather than only per run.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise AINotConfigured('ANTHROPIC_API_KEY is not set.')

    from anthropic import Anthropic
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    # Copied, never mutated in place — the caller's stored history must
    # not grow a turn as a side effect of a run that then fails.
    messages = list(prior_messages or [])
    messages.append({'role': 'user', 'content': user_message})
    transcript = []
    final_text = ''
    stopped_reason = 'max_steps'
    steps_used = 0

    for step in range(max_steps):
        steps_used = step + 1
        kwargs = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': messages,
            'tools': tools,
        }
        if system:
            kwargs['system'] = system
        if effort:
            kwargs['output_config'] = {'effort': effort}

        try:
            response = _agent_api_call(
                client, kwargs, max_tokens, on_text=on_text)
        except Exception as exc:  # noqa: BLE001
            logger.exception('claude_agent_loop: API call failed on step %s',
                             steps_used)
            transcript.append({'type': 'error', 'detail': str(exc)})
            return {
                'transcript': transcript, 'final_text': final_text,
                'stopped_reason': 'error', 'steps_used': steps_used,
            }

        _record_agent_usage(response, model, on_usage)

        text_blocks, tool_uses = [], []
        for block in response.content:
            btype = getattr(block, 'type', '')
            if btype == 'text':
                text_blocks.append(getattr(block, 'text', '') or '')
            elif btype == 'tool_use':
                tool_uses.append(block)

        if text_blocks:
            final_text = '\n'.join(t.strip() for t in text_blocks if t.strip())
            transcript.append({'type': 'text', 'text': final_text})

        # No tool calls => Claude is done.
        if not tool_uses:
            stopped_reason = 'done'
            # Record the closing turn before leaving. It is the one
            # carrying the ANSWER, and until this existed `messages`
            # ended on a dangling tool_result with the reply present only
            # in `final_text`. A one-shot run never noticed — it reads
            # final_text. Resuming that history in a chat did: the reply
            # was missing from the thread, and the next turn appended a
            # second user message straight after the first.
            messages.append({
                'role': 'assistant',
                'content': _serialise_content_blocks(response.content),
            })
            break

        # Echo the assistant turn back verbatim — dropping tool_use blocks
        # breaks the pairing the API requires on the next request.
        # Serialised to plain dicts rather than kept as SDK objects so the
        # whole `messages` list stays JSON-round-trippable AND replayable:
        # it is persisted to AIEmployeeRun.message_history and is what a
        # future chat pane would pass back in.
        messages.append({
            'role': 'assistant',
            'content': _serialise_content_blocks(response.content),
        })

        # Execute every requested tool, then return ALL results in ONE
        # user message. Splitting them across messages silently teaches
        # the model to stop making parallel calls.
        results = []
        for tu in tool_uses:
            name = getattr(tu, 'name', '')
            tool_input = getattr(tu, 'input', {}) or {}
            output, is_error = _run_agent_tool(tool_executor, name, tool_input)
            transcript.append({
                'type': 'tool_use', 'name': name,
                'input': tool_input, 'result': output,
                'is_error': is_error,
            })
            results.append({
                'type': 'tool_result',
                'tool_use_id': getattr(tu, 'id', ''),
                'content': output,
                'is_error': is_error,
            })
        messages.append({'role': 'user', 'content': results})
    else:
        logger.warning(
            'claude_agent_loop: hit max_steps=%s without finishing', max_steps)

    return {
        'transcript': transcript, 'final_text': final_text,
        'stopped_reason': stopped_reason, 'steps_used': steps_used,
        # The real conversation, in Anthropic wire shape. Persist this —
        # see _serialise_content_blocks for why it matters.
        'messages': messages,
    }


def _serialise_content_blocks(content):
    """Turn SDK content blocks into plain JSON-safe dicts.

    The blocks the SDK hands back (TextBlock, ToolUseBlock, ThinkingBlock)
    are Pydantic objects. They are accepted on the way back in, but they
    are not JSON-serialisable, so a `messages` list containing them cannot
    be stored.

    We convert to dicts as the conversation is built, which buys two
    things at once: the list stays valid to send back to the API, and it
    can be persisted verbatim to ``AIEmployeeRun.message_history``.

    That matters for a conversational chat pane later. Adding one means
    passing prior turns back into the loop, which needs real message
    objects — tool_use and tool_result blocks included. Reconstructing
    those from a flattened summary string afterwards is lossy and would
    cost a migration plus a backfill. Storing the right shape now costs a
    JSONField.

    Thinking blocks are preserved with their signature intact; they must
    be passed back unmodified when continuing on the same model.
    """
    out = []
    for block in content or []:
        if isinstance(block, dict):
            out.append(block)
            continue
        dump = getattr(block, 'model_dump', None)
        if callable(dump):
            try:
                out.append(dump(exclude_none=True))
                continue
            except Exception:  # noqa: BLE001
                pass
        # Last-resort hand rebuild for anything without model_dump.
        btype = getattr(block, 'type', '')
        if btype == 'text':
            out.append({'type': 'text', 'text': getattr(block, 'text', '')})
        elif btype == 'tool_use':
            out.append({
                'type': 'tool_use',
                'id': getattr(block, 'id', ''),
                'name': getattr(block, 'name', ''),
                'input': getattr(block, 'input', {}) or {},
            })
        else:
            logger.warning(
                'claude_agent_loop: dropping unserialisable %r block from '
                'stored history', btype)
    return out


def _agent_api_call(client, kwargs, max_tokens, on_text=None):
    """One API call.

    Streams when the caller wants text deltas, or when max_tokens is
    large enough that a non-streaming request would risk the SDK's HTTP
    timeout. ``text_stream`` yields visible text only — thinking blocks
    are not emitted through it, which is what we want: thinking is
    working, not answer.

    A failing ``on_text`` is logged and swallowed. Rendering is not worth
    losing a reply that has already been paid for.
    """
    if on_text is None and max_tokens <= _STREAMING_THRESHOLD_TOKENS:
        return client.messages.create(**kwargs)

    with client.messages.stream(**kwargs) as stream:
        if on_text is not None:
            for chunk in stream.text_stream:
                try:
                    on_text(chunk)
                except Exception:  # noqa: BLE001
                    logger.exception('claude_agent_loop: on_text failed')
        return stream.get_final_message()


def _record_agent_usage(response, model, on_usage):
    """Book one call's tokens to ClaudeUsage and to the caller's hook.

    Best-effort on both counts — accounting must never take down a run —
    but failures are logged loudly, because unrecorded spend is spend the
    daily cap cannot see.
    """
    u = getattr(response, 'usage', None)
    if u is None:
        return
    input_tokens = getattr(u, 'input_tokens', 0) or 0
    output_tokens = getattr(u, 'output_tokens', 0) or 0

    try:
        from reporting.models import ClaudeUsage
        ClaudeUsage.record(
            model=model, input_tokens=input_tokens,
            output_tokens=output_tokens)
    except Exception:
        logger.exception('claude_agent_loop: ClaudeUsage.record failed')

    if on_usage is not None:
        try:
            on_usage(model, input_tokens, output_tokens)
        except Exception:
            logger.exception(
                'claude_agent_loop: on_usage hook failed — this run spend '
                'may be under-counted against the daily cap')


def _run_agent_tool(tool_executor, name, tool_input):
    """Invoke one tool. Returns ``(output_str, is_error)``.

    A tool that raises becomes an error RESULT rather than an exception:
    the model gets told what went wrong and can adapt, which is the whole
    point of an agent loop. A raise here would throw away every step the
    run had already completed.
    """
    try:
        output = tool_executor(name, tool_input)
    except Exception as exc:  # noqa: BLE001
        logger.exception('claude_agent_loop: tool %r raised', name)
        return f'Tool {name} failed: {exc}', True

    if isinstance(output, str):
        return output, False
    try:
        import json as _json
        return _json.dumps(output, default=str), False
    except Exception:  # noqa: BLE001
        return str(output), False
