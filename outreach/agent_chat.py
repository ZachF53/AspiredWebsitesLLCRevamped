"""
Prospect as something you can talk to — COLD_OUTREACH_AGENT.md §5.1's
"conversational chat — future-proofed, not built", now built.

WHAT THIS ADDS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
A chat turn is a normal agent run. ``AIEmployeeRun`` gains a
``conversation`` FK and a 'chat' trigger, and everything that already
guards a scheduled run guards a chat turn unchanged:

    spend.check_spend_allowed()   the daily dollar cap
    agent_tools COMMIT class      human approval on money and mail
    make_executor(run)            one AIEmployeeAction per tool call

None of that is reimplemented here. A tool that costs money still files
for approval instead of running, whether Prospect decided to call it on a
schedule or because you asked it a question — being asked in a chat box
is not consent to spend.

WHY THE WIRE FORMAT IS THE STORED FORMAT
----------------------------------------
``AIEmployeeConversation.messages`` holds Anthropic's real message
shape — tool_use blocks with their ids, tool_result blocks that pair to
them, thinking blocks with signatures intact. The rendered transcript is
derived from it on read.

The temptation is to store a tidy list of {who, text} for the template
and keep the wire format alongside. Don't: the two drift, and the API
rejects a turn whose tool_use ids do not pair with their results. There
is one copy, it is the one the model must agree with, and display is
computed from it.

TITLES
------
The first user message becomes the thread title, truncated. No extra
Claude call — a title is worth about zero cents and the first line of
what you typed is a better label than a model's paraphrase of it.
"""

import logging
import time

from django.utils import timezone

from outreach import spend
from outreach.agent_runtime import IDENTITY, PROSPECT_SLUG

logger = logging.getLogger(__name__)


# A chat turn is bounded lower than a scheduled run. A scheduled run has
# a whole funnel to work through; a chat turn is answering one question,
# and a model that needs more than six tool calls for that has usually
# misunderstood rather than found more work.
CHAT_MAX_STEPS = 6

TITLE_MAX = 80

# Withheld from chat, not merely discouraged in the prompt. write_journal
# overwrites AIEmployee.last_journal_entry, which is what the next
# SCHEDULED run reads as its only memory. A chat answering "how's Houston
# going?" must not be able to erase what the 2am run learned.
CHAT_WITHHELD_TOOLS = ('write_journal',)


CHAT_SYSTEM_PROMPT = IDENTITY + """

YOU ARE IN A CHAT
Zachery is talking to you directly and waiting for the reply. This is a
conversation, not a scheduled run.

  - Answer what was actually asked. Do not deliver a status report
    nobody requested.
  - Use your tools to get real numbers before answering anything factual
    about the funnel. "I think roughly" is never acceptable when
    funnel_status exists.
  - You remember this conversation — earlier turns are above. Do not
    re-read the funnel to answer a follow-up about something you just
    read, and do not reintroduce yourself.
  - Do NOT call write_journal. That is for scheduled runs; here your
    memory is the conversation itself, and a journal entry written from a
    chat would overwrite what your last real run learned.
  - If asked to do something that needs approval, say plainly that you
    have filed it and it has NOT happened yet.
  - If you do not know, say so and say which tool would tell you.

Write like a competent colleague answering across a desk. Short
paragraphs. No headers, no bullet-point walls, no restating the question
before answering it.\
"""


def _employee():
    from admin_dashboard.models import AIEmployee
    return AIEmployee.objects.filter(slug=PROSPECT_SLUG).first()


def title_from(text):
    """First line of what was typed, trimmed to fit the sidebar."""
    line = ' '.join((text or '').strip().split())
    if len(line) <= TITLE_MAX:
        return line
    return line[:TITLE_MAX - 1].rstrip() + '…'


def start_conversation(employee=None, user=None):
    """Open an empty thread. The first send titles it."""
    from admin_dashboard.models import AIEmployeeConversation

    employee = employee or _employee()
    if employee is None:
        return None
    return AIEmployeeConversation.objects.create(
        employee=employee,
        started_by=user if (user and user.is_authenticated) else None,
    )


def queue_turn(conversation, text):
    """Record the user's message and create the run that will answer it.

    Returns ``(run, error)``. The run is created in 'running' state and
    the message is appended to the thread BEFORE any model call, so the
    page can render what you said immediately and poll for the answer.

    Refusals (no API key, daily cap reached) come back as ``error`` with
    no run, because a run row that never ran makes the log lie.
    """
    from admin_dashboard.models import AIEmployeeRun
    from reporting import ai

    text = (text or '').strip()
    if not text:
        return None, 'Type a message first.'

    if not ai.is_configured():
        return None, 'ANTHROPIC_API_KEY is not set — Prospect cannot reply.'

    allowed, why = spend.check_spend_allowed()
    if not allowed:
        return None, why

    if not conversation.title:
        conversation.title = title_from(text)

    # Appended here, not in the worker: you should see your own message
    # the instant you send it, whether or not a worker ever picks the
    # turn up.
    conversation.messages = list(conversation.messages or []) + [
        {'role': 'user', 'content': text}]
    conversation.save(update_fields=['title', 'messages', 'updated_at'])

    run = AIEmployeeRun.objects.create(
        employee=conversation.employee,
        conversation=conversation,
        trigger='chat',
    )
    return run, ''


class _PartialWriter:
    """Persist streamed text often enough to look live, rarely enough
    not to write to the database once per token.

    A turn producing 400 tokens would otherwise be 400 UPDATEs. At a
    0.4s floor it is a handful, and the page polls at roughly the same
    rate, so nothing is lost by being coarser than the token stream.
    """

    FLUSH_SECONDS = 0.4

    def __init__(self, run):
        self.run = run
        self.chunks = []
        self._last_flush = 0.0

    def __call__(self, chunk):
        self.chunks.append(chunk)
        now = time.monotonic()
        if now - self._last_flush >= self.FLUSH_SECONDS:
            self.flush()

    def flush(self):
        self._last_flush = time.monotonic()
        text = ''.join(self.chunks)
        try:
            type(self.run).objects.filter(pk=self.run.pk).update(
                partial_text=text)
        except Exception:  # noqa: BLE001
            logger.exception('could not persist streamed text for run %s',
                             self.run.pk)


def run_chat_turn(run_id):
    """Answer the last user message on this run's conversation.

    Callable directly (tests, a shell, a synchronous fallback) or from
    the Celery task that wraps it. Never raises for an operational
    reason — the failure is written to the run and rendered in the
    thread, because a chat that silently stops is indistinguishable from
    one that is still thinking.
    """
    from admin_dashboard.models import AIEmployeeRun
    from outreach.agent_tools import anthropic_tools, make_executor
    from reporting import ai

    run = (AIEmployeeRun.objects
           .select_related('employee', 'conversation')
           .filter(pk=run_id).first())
    if run is None:
        return 'no such run'
    conversation = run.conversation
    if conversation is None:
        return 'run is not part of a conversation'

    def fail(message):
        run.status = 'failed'
        run.summary = message
        run.partial_text = ''
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'summary', 'partial_text',
                                'finished_at'])
        return message

    history = list(conversation.messages or [])
    if not history or history[-1].get('role') != 'user':
        return fail('Nothing to answer — no user message on this thread.')

    # The loop appends `user_message` itself, so hand it the turns BEFORE
    # the one being answered and the text separately. Passing the whole
    # history plus the same text again would duplicate the question.
    prior = history[:-1]
    latest = history[-1].get('content')
    if not isinstance(latest, str):
        return fail('The last turn is not a plain user message.')

    def on_usage(model, input_tokens, output_tokens):
        cost = spend.claude_call_cost_usd(model, input_tokens, output_tokens)
        run.spend_usd = (run.spend_usd or 0) + cost
        run.save(update_fields=['spend_usd'])

    streamer = _PartialWriter(run)
    try:
        result = ai.claude_agent_loop(
            system=CHAT_SYSTEM_PROMPT,
            tools=anthropic_tools(exclude=CHAT_WITHHELD_TOOLS),
            tool_executor=make_executor(run),
            user_message=latest,
            prior_messages=prior,
            model=ai.MODEL_CONTENT,
            max_steps=CHAT_MAX_STEPS,
            effort=run.employee.reasoning_effort,
            on_usage=on_usage,
            on_text=streamer,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('run_chat_turn: agent loop failed on run %s', run_id)
        return fail(f'Prospect hit an error: {exc}')

    messages = result.get('messages') or []
    final_text = (result.get('final_text') or '').strip()

    if result.get('stopped_reason') == 'error' and not final_text:
        return fail('Prospect could not reach the Claude API. Try again.')

    # The loop returns the FULL list it worked with — prior turns plus
    # this turn's. Storing it wholesale is what keeps ids paired; there
    # is no need to splice, and splicing is how pairing gets broken.
    if messages:
        conversation.messages = messages
    if result.get('stopped_reason') == 'max_steps' and not final_text:
        final_text = (
            'I used all my tool calls for this turn without landing on an '
            'answer. Ask me again more narrowly.')
        conversation.messages = list(conversation.messages) + [
            {'role': 'assistant', 'content': final_text}]

    conversation.save(update_fields=['messages', 'updated_at'])

    run.steps_used = result.get('steps_used', 0)
    run.message_history = messages
    run.summary = final_text[:8000]
    run.status = 'completed'
    # Cleared deliberately. From here the settled answer lives in the
    # conversation, and leaving a copy on the run would give the fragment
    # two sources for the same reply to disagree about.
    run.partial_text = ''
    run.finished_at = timezone.now()
    run.save(update_fields=['steps_used', 'message_history', 'summary',
                            'status', 'partial_text', 'finished_at'])
    return 'ok'


# ── Approving a COMMIT call from inside the conversation ───────────────
#
# The COMMIT class still means "a human decides". What changed is WHERE
# and WHEN: the decision is made in the thread that asked for it, and
# approval executes the work immediately instead of parking it until the
# next scheduled wake-up.
#
# That parking was correct for a nightly agent and useless for a chat.
# Asking Prospect to source San Antonio, approving it, and then having to
# find the detail page and click "Wake now" is three screens to do one
# thing, and the middle screen exists only because the original design
# had no conversation to put the button in.
#
# What did NOT change: the model cannot approve its own call, nothing is
# executed without a human click, and the spend caps still apply.

def run_approved_action(action_id):
    """Execute one approved COMMIT call and report back into its thread.

    Returns a short status string. Never raises for an operational
    reason — the failure is written where the person who clicked Approve
    is looking.
    """
    from admin_dashboard.models import AIEmployeeAction, AIEmployeeRun
    from outreach.agent_tools import action_reporter, _resolve

    action = (AIEmployeeAction.objects
              .select_related('run', 'run__conversation', 'run__employee')
              .filter(pk=action_id).first())
    if action is None:
        return 'no such action'
    if not action.approved:
        return 'not approved'
    if action.executed_at:
        # The idempotence guard. Approval is permanent, so without this a
        # double-click or a retried task charges the card twice.
        return 'already executed'

    conversation = action.run.conversation
    impl = _resolve(action.tool_name)
    if impl is None:
        return f'no implementation for {action.tool_name}'

    # A separate run, so the work has its own spend row and its own
    # progress, and is not confused with the turn that merely asked.
    run = AIEmployeeRun.objects.create(
        employee=action.run.employee,
        conversation=conversation,
        trigger='chat',
        status='running',
    )
    action.run = run
    action.save(update_fields=['run'])
    report = action_reporter(action)

    try:
        result = impl(action.tool_input or {}, report)
        ok = True
    except Exception as exc:  # noqa: BLE001
        logger.exception('approved action %s failed', action_id)
        result = f'{action.tool_name} failed: {exc}'
        ok = False

    action.result = str(result)[:4000]
    action.executed_at = timezone.now()
    action.save(update_fields=['result', 'executed_at'])

    run.status = 'completed' if ok else 'failed'
    run.summary = f'{action.tool_name}: {str(result)[:2000]}'
    run.finished_at = timezone.now()
    run.save(update_fields=['status', 'summary', 'finished_at'])

    if conversation is not None:
        _append_system_turn(
            conversation,
            f'[{action.tool_name} was approved and has now RUN. '
            f'Result: {result}]')
    return 'ok' if ok else 'failed'


def _append_system_turn(conversation, text):
    """Put an out-of-band fact into the thread as a user turn.

    A user turn rather than an assistant one on purpose: it is
    information given TO Prospect, not something it said. Writing it as
    an assistant turn would let it later cite its own summary as though
    it had observed the result itself.
    """
    conversation.messages = list(conversation.messages or []) + [
        {'role': 'user', 'content': text}]
    conversation.save(update_fields=['messages', 'updated_at'])


# ── Rendering ──────────────────────────────────────────────────────────

def render_transcript(messages, actions=None):
    """Wire-format thread -> what the template draws.

    Returns a list of
    ``{'kind', 'text', 'tool_name', 'tool_input', 'progress'}``.

    The subtlety worth stating: a ``user`` turn whose content is a LIST
    is not something a human said — it is the tool_result block answering
    the assistant's tool_use. Rendering it as a user bubble would put
    JSON in the chat under Zachery's name.

    ``actions`` are this conversation's AIEmployeeAction rows in creation
    order, which is the order their tool_use blocks appear. They carry
    the progress lines a long tool emitted. Matched positionally but
    CHECKED by name: attaching "82 imported" under the wrong tool is
    worse than showing no progress at all, so a mismatch drops the
    attachment instead of guessing.
    """
    pending_actions = list(actions or [])
    out = []
    for msg in messages or []:
        role = msg.get('role')
        content = msg.get('content')

        if role == 'user':
            if isinstance(content, str):
                out.append({'kind': 'user', 'text': content,
                            'tool_name': '', 'tool_input': None,
                            'progress': []})
            # A list here is tool_result blocks — the results are already
            # shown against the tool_use that asked for them.
            continue

        if role != 'assistant':
            continue

        if isinstance(content, str):
            if content.strip():
                out.append({'kind': 'assistant', 'text': content,
                            'tool_name': '', 'tool_input': None,
                            'progress': []})
            continue

        for block in content or []:
            btype = block.get('type') if isinstance(block, dict) else None
            if btype == 'text':
                text = (block.get('text') or '').strip()
                if text:
                    out.append({'kind': 'assistant', 'text': text,
                                'tool_name': '', 'tool_input': None,
                                'progress': []})
            elif btype == 'tool_use':
                name = block.get('name') or 'tool'
                progress = []
                if pending_actions:
                    if pending_actions[0].tool_name == name:
                        progress = list(pending_actions.pop(0).progress or [])
                    else:
                        # Out of step. Drop this action rather than hang
                        # its lines on a tool that did not produce them.
                        pending_actions.pop(0)
                out.append({
                    'kind': 'tool',
                    'text': '',
                    'tool_name': name,
                    'tool_input': block.get('input') or {},
                    'progress': progress,
                })
            # Thinking blocks are stored (the API needs their signatures
            # on replay) but not rendered — they are working, not answer.
    return out
