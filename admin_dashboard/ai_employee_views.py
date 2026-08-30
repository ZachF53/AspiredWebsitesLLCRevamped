"""
AI Employees — the cockpit for agents like Prospect
(COLD_OUTREACH_AGENT.md §8.2).

Read the honesty note before extending this page.

WHAT WORKS TODAY vs WHAT DOES NOT
---------------------------------
The agent RUNTIME (§5.2 tools, §5.3 memory, §5.4 scheduling) is not built.
Prospect exists as a registry row with guardrails and a loop primitive
behind it, and nothing that wakes it up.

So this page deliberately distinguished the two while the runtime was
missing: pause/resume and task assignment were real, and "Wake now" was
rendered DISABLED with the reason stated, because a button that appears
to work and silently does nothing is worse than one that says why it
cannot.

The runtime now exists — outreach/agent_tools.py (the tool registry and
approval gate) and outreach/agent_runtime.py (run_prospect) — so
RUNTIME_READY is True and the button is live. It queues a Celery task
rather than running inline; the run log below is where the result
appears.

What is still worth knowing when reading this page: a COMMIT-class tool
call (a paid scrape, or a push that emails strangers) shows up here as
an action AWAITING APPROVAL and has NOT run. Approving it records the
decision; the work itself happens on the agent's next run.
"""

import logging

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.context import _admin_context
from admin_dashboard.decorators import admin_required

logger = logging.getLogger(__name__)
from admin_dashboard.models import (
    AIEmployee,
    AIEmployeeAction,
    AIEmployeeConversation,
    AIEmployeeRun,
    AIEmployeeTask,
)


# §5.2 (tools) and §5.4 (the run entrypoint) are built:
#   outreach/agent_tools.py    the registry, executor and approval gate
#   outreach/agent_runtime.py  run_prospect()
#   beat entry 'prospect-agent'
#
# Kept as a single named constant so "is the runtime built?" has exactly
# one answer rather than being re-derived in a template condition.
RUNTIME_READY = True

RUNTIME_PENDING_REASON = (
    'The agent runtime is not built yet — Prospect has no tools to call '
    'and nothing to wake it. Assigning a task below still works: it waits '
    'in the queue and is picked up on the first run.'
)


def _pending_actions_qs(employee=None):
    """Tool calls awaiting a human decision.

    This is the ONLY thing the AI Employees badge counts. EmailSent rows
    in `pending_approval` stay with the existing Approvals nav item — the
    same queue under two badges would double-count the operator's work.
    """
    qs = AIEmployeeAction.objects.filter(
        requires_approval=True, approved__isnull=True)
    if employee is not None:
        qs = qs.filter(run__employee=employee)
    return qs


@admin_required
def ai_employees(request):
    """One card per agent."""
    employees = list(
        AIEmployee.objects.annotate(
            run_count=Count('runs', distinct=True),
            open_tasks=Count(
                'tasks', filter=Q(tasks__status='pending'), distinct=True),
        ).order_by('name')
    )

    # Latest run per employee, and pending-approval counts. Done as two
    # small queries rather than a subquery per card — this list is single
    # digits and will stay that way.
    latest = {}
    for run in AIEmployeeRun.objects.order_by('employee_id', '-started_at'):
        latest.setdefault(run.employee_id, run)

    pending = dict(
        _pending_actions_qs()
        .values_list('run__employee')
        .annotate(n=Count('id'))
    )

    for e in employees:
        e.latest_run = latest.get(e.pk)
        e.pending_approvals = pending.get(e.pk, 0)

    return render(request, 'admin_dashboard/ai_employees.html', _admin_context(
        active='ai_employees',
        employees=employees,
        runtime_ready=RUNTIME_READY,
        runtime_pending_reason=RUNTIME_PENDING_REASON,
    ))


@admin_required
def ai_employee_detail(request, slug):
    """Run log, pending decisions, and task assignment for one agent."""
    employee = get_object_or_404(AIEmployee, slug=slug)

    runs = list(
        AIEmployeeRun.objects.filter(employee=employee)
        .prefetch_related('actions')
        .order_by('-started_at')[:25]
    )
    tasks = list(
        AIEmployeeTask.objects.filter(employee=employee)
        .order_by('status', '-created_at')[:25]
    )
    pending_actions = list(
        _pending_actions_qs(employee)
        .select_related('run')
        .order_by('created_at')
    )

    return render(
        request, 'admin_dashboard/ai_employee_detail.html', _admin_context(
            active='ai_employees',
            employee=employee,
            runs=runs,
            tasks=tasks,
            pending_actions=pending_actions,
            runtime_ready=RUNTIME_READY,
            runtime_pending_reason=RUNTIME_PENDING_REASON,
        ))


@admin_required
@require_POST
def ai_employee_toggle_active(request, slug):
    """Pause or resume scheduled runs. Real — this persists."""
    employee = get_object_or_404(AIEmployee, slug=slug)
    employee.active = not employee.active
    employee.save(update_fields=['active'])
    messages.success(
        request,
        f'{employee.name} is now '
        f'{"active" if employee.active else "paused"}.')
    return redirect('admin_dashboard:ai_employee_detail', slug=slug)


@admin_required
@require_POST
def ai_employee_add_task(request, slug):
    """Queue a manual instruction. Real — it waits for the first run."""
    employee = get_object_or_404(AIEmployee, slug=slug)
    instruction = (request.POST.get('instruction') or '').strip()
    if not instruction:
        messages.error(request, 'Enter an instruction before assigning it.')
        return redirect('admin_dashboard:ai_employee_detail', slug=slug)

    AIEmployeeTask.objects.create(
        employee=employee,
        instruction=instruction,
        created_by=request.user if request.user.is_authenticated else None,
    )
    messages.success(
        request,
        f'Task queued for {employee.name}. It stays pending until the '
        f'agent runtime is built and its first run picks it up.')
    return redirect('admin_dashboard:ai_employee_detail', slug=slug)


@admin_required
@require_POST
def ai_employee_wake(request, slug):
    """Trigger a manual run.

    Refuses while RUNTIME_READY is False rather than creating an
    AIEmployeeRun that does nothing — an empty run row would make the log
    look like the agent works.
    """
    employee = get_object_or_404(AIEmployee, slug=slug)
    if not RUNTIME_READY:
        messages.error(
            request,
            f'Cannot wake {employee.name}: {RUNTIME_PENDING_REASON}')
        return redirect('admin_dashboard:ai_employee_detail', slug=slug)

    # Queued, not run inline. A run makes several Claude calls and can
    # take minutes; doing that in the request would hold a gunicorn
    # worker and time out behind nginx. The run log is where the result
    # appears.
    from outreach.tasks import run_prospect_task

    try:
        run_prospect_task.delay(trigger='manual')
        messages.success(
            request,
            f'{employee.name} is waking up. Refresh in a minute — the run '
            f'and everything it did will appear below.')
    except Exception as exc:  # noqa: BLE001
        # Almost always Redis being down. Say which, because "could not
        # wake" sends someone looking at the agent instead of the broker.
        logger.exception('could not queue a manual run for %s', employee.slug)
        messages.error(
            request,
            f'Could not queue the run — the task broker did not accept it '
            f'({exc}). Check that Redis and the Celery worker are running.')

    return redirect('admin_dashboard:ai_employee_detail', slug=slug)


@admin_required
@require_POST
def ai_action_decide(request, action_id):
    """Approve or reject one tool call that asked for a human."""
    action = get_object_or_404(
        AIEmployeeAction, pk=action_id,
        requires_approval=True, approved__isnull=True)

    decision = (request.POST.get('decision') or '').strip()
    if decision not in ('approve', 'reject'):
        messages.error(request, 'Unknown decision.')
        return redirect(
            'admin_dashboard:ai_employee_detail',
            slug=action.run.employee.slug)

    from django.utils import timezone
    action.approved = (decision == 'approve')
    action.approved_by = (
        request.user if request.user.is_authenticated else None)
    action.approved_at = timezone.now()
    action.save(update_fields=['approved', 'approved_by', 'approved_at'])

    messages.success(
        request,
        f'{action.tool_name} '
        f'{"approved" if action.approved else "rejected"}.')
    return redirect(
        'admin_dashboard:ai_employee_detail',
        slug=action.run.employee.slug)


# ── Chat (COLD_OUTREACH_AGENT.md §5.1) ─────────────────────────────────
#
# Talking to Prospect rather than reading what it did overnight. Every
# message is a normal AIEmployeeRun with trigger='chat', so the spend
# ledger, the action log and the approval queue all keep working with no
# parallel machinery.
#
# WHY THE ANSWER ARRIVES BY POLLING
# A turn makes several Claude calls and can take a minute. Doing that in
# the POST would hold a gunicorn worker and time out behind nginx at 30s.
# So: the POST records your message and queues the work, then the
# messages fragment polls itself until the run stops running. The
# fragment carries its own hx-trigger only while a turn is in flight, so
# an idle thread makes no requests at all.


def _conversation(slug, conversation_id):
    return get_object_or_404(
        AIEmployeeConversation,
        pk=conversation_id, employee__slug=slug)


def _live_context(conversation):
    """Just the in-flight part of a thread.

    Polled on its own so a streaming reply does not re-render the whole
    transcript every second — swapping the entire thread that often
    destroys text selection and makes long threads flicker.
    """
    run = conversation.runs.filter(status='running').first()
    return {
        'conversation': conversation,
        'pending': run is not None,
        'streamed_text': (run.partial_text if run else '') or '',
        # What the running tool is saying about itself, so a four-minute
        # scrape shows its stages instead of a spinner.
        'live_actions': list(
            run.actions.order_by('created_at')) if run else [],
        # COMMIT calls waiting on a click, rendered in the thread that
        # asked for them.
        'awaiting_approval': list(
            AIEmployeeAction.objects
            .filter(run__conversation=conversation,
                    requires_approval=True, approved__isnull=True)
            .order_by('created_at')),
        # True only when this fragment was fetched by the poll. The
        # finished branch does a one-shot reload of the whole thread, and
        # the whole thread includes this fragment again — so if that
        # branch rendered inside a thread render too, the two would fetch
        # each other forever.
        'live_poll': True,
    }


def _thread_context(conversation, error=''):
    """What both the full page and the polled fragment need."""
    from outreach.agent_chat import render_transcript

    live = _live_context(conversation)
    pending = live['pending']
    last_failed = (conversation.runs.filter(status='failed')
                   .order_by('-started_at').first())
    # Progress lines are attached to the settled transcript by walking
    # the actions in the order they were created, which is the order the
    # tool_use blocks appear. render_transcript refuses to attach when
    # the names disagree rather than guessing.
    actions = list(
        AIEmployeeAction.objects
        .filter(run__conversation=conversation)
        .order_by('created_at'))
    return {
        'conversation': conversation,
        'transcript': render_transcript(conversation.messages,
                                        actions=actions),
        'pending': pending,
        'streamed_text': live['streamed_text'],
        'live_actions': live['live_actions'],
        'awaiting_approval': live['awaiting_approval'],
        # See _live_context: the one-shot reload belongs to the poll only.
        'live_poll': False,
        'chat_error': error,
        # Surfaced only when it is the most recent thing that happened,
        # so an old failure does not sit under a healthy thread forever.
        'last_failure': (
            last_failed if (last_failed and not pending
                            and last_failed == conversation.runs.first())
            else None),
    }


def _conversation_list(employee):
    return list(
        AIEmployeeConversation.objects
        .filter(employee=employee, archived=False)
        .order_by('-updated_at')[:50]
    )


@admin_required
def ai_employee_chat(request, slug, conversation_id=None):
    """The chat page: thread history on the left, one conversation open."""
    employee = get_object_or_404(AIEmployee, slug=slug)
    conversations = _conversation_list(employee)

    conversation = None
    if conversation_id is not None:
        conversation = _conversation(slug, conversation_id)
    elif conversations:
        conversation = conversations[0]

    ctx = {'conversation': None, 'transcript': [], 'pending': False,
           'chat_error': '', 'last_failure': None}
    if conversation is not None:
        ctx = _thread_context(conversation)

    return render(request, 'admin_dashboard/ai_employee_chat.html',
                  _admin_context(
                      active='ai_employees',
                      employee=employee,
                      conversations=conversations,
                      runtime_ready=RUNTIME_READY,
                      **ctx))


@admin_required
@require_POST
def ai_chat_new(request, slug):
    """Start an empty thread and open it."""
    from outreach.agent_chat import start_conversation

    employee = get_object_or_404(AIEmployee, slug=slug)
    conversation = start_conversation(employee=employee, user=request.user)
    return redirect('admin_dashboard:ai_employee_chat_thread',
                    slug=slug, conversation_id=conversation.pk)


@admin_required
@require_POST
def ai_chat_send(request, slug, conversation_id):
    """Record a message and queue the turn that answers it."""
    from outreach.agent_chat import queue_turn

    conversation = _conversation(slug, conversation_id)

    # One turn at a time. Two in flight would race on
    # conversation.messages and the loser's answer would vanish.
    if conversation.runs.filter(status='running').exists():
        return render(request, 'admin_dashboard/_chat_thread.html',
                      _thread_context(
                          conversation,
                          error='Prospect is still answering — wait for '
                                'this reply before sending another.'))

    run, error = queue_turn(conversation, request.POST.get('message'))
    if error:
        return render(request, 'admin_dashboard/_chat_thread.html',
                      _thread_context(conversation, error=error))

    from outreach.tasks import run_chat_turn_task
    try:
        run_chat_turn_task.delay(run.pk)
    except Exception as exc:  # noqa: BLE001
        # Almost always Redis. Fail the run rather than leaving it
        # 'running' forever with the fragment polling a corpse.
        logger.exception('could not queue chat turn %s', run.pk)
        run.status = 'failed'
        run.summary = (
            f'Could not queue the reply — the task broker did not accept '
            f'it ({exc}). Check Redis and the Celery worker.')
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'summary', 'finished_at'])

    conversation.refresh_from_db()
    return render(request, 'admin_dashboard/_chat_thread.html',
                  _thread_context(conversation))


@admin_required
def ai_chat_thread_fragment(request, slug, conversation_id):
    """The whole thread. Rendered on send and once a turn completes."""
    conversation = _conversation(slug, conversation_id)
    return render(request, 'admin_dashboard/_chat_thread.html',
                  _thread_context(conversation))


@admin_required
def ai_chat_live_fragment(request, slug, conversation_id):
    """The streaming bubble, polled about once a second while a turn runs.

    Small on purpose: it carries only the text arriving now, so the poll
    stays cheap and the settled transcript above it is left alone. When
    the run stops, the fragment asks once for the full thread and stops
    polling — that final swap is what moves the answer from "streaming"
    into the conversation proper.
    """
    conversation = _conversation(slug, conversation_id)
    return render(request, 'admin_dashboard/_chat_live.html',
                  _live_context(conversation))


@admin_required
@require_POST
def ai_chat_decide(request, slug, conversation_id, action_id):
    """Approve or reject a COMMIT call from inside the conversation.

    Unlike ai_action_decide, approving here EXECUTES. That is the whole
    point: parking approved work until the next scheduled wake-up made
    sense for a nightly agent and made the chat useless — you would
    approve a scrape and nothing would happen until morning.

    The execution still does not happen in this request. It is queued,
    and the thread polls for it exactly as it does for a reply, because a
    paid scrape inside a request/response cycle would block the worker
    and a timeout would leave "did it charge me?" unanswerable.
    """
    conversation = _conversation(slug, conversation_id)
    action = get_object_or_404(
        AIEmployeeAction, pk=action_id,
        run__conversation=conversation,
        requires_approval=True, approved__isnull=True)

    approve = (request.POST.get('decision') or '') == 'approve'
    action.approved = approve
    action.approved_by = (
        request.user if request.user.is_authenticated else None)
    action.approved_at = timezone.now()
    action.save(update_fields=['approved', 'approved_by', 'approved_at'])

    if not approve:
        from outreach.agent_chat import _append_system_turn
        _append_system_turn(
            conversation,
            f'[{action.tool_name} was REJECTED by Zachery and will not '
            f'run. Do not ask for it again unless something changes.]')
        conversation.refresh_from_db()
        return render(request, 'admin_dashboard/_chat_thread.html',
                      _thread_context(conversation))

    from outreach.tasks import run_approved_action_task
    try:
        run_approved_action_task.delay(action.pk)
    except Exception as exc:  # noqa: BLE001
        logger.exception('could not queue approved action %s', action.pk)
        # Hand the approval back rather than leaving a row marked
        # approved that nothing will ever execute.
        action.approved = None
        action.approved_at = None
        action.save(update_fields=['approved', 'approved_at'])
        return render(request, 'admin_dashboard/_chat_thread.html',
                      _thread_context(
                          conversation,
                          error=f'Could not start it — the task broker '
                                f'refused ({exc}). Check Redis and the '
                                f'Celery worker, then approve again.'))

    conversation.refresh_from_db()
    return render(request, 'admin_dashboard/_chat_thread.html',
                  _thread_context(conversation))


@admin_required
@require_POST
def ai_chat_archive(request, slug, conversation_id):
    """Hide a thread from the sidebar. Never deletes — the runs, actions
    and spend attached to it stay auditable."""
    conversation = _conversation(slug, conversation_id)
    conversation.archived = True
    conversation.save(update_fields=['archived', 'updated_at'])
    messages.success(request, 'Conversation archived.')
    return redirect('admin_dashboard:ai_employee_chat', slug=slug)


@admin_required
@require_POST
def ai_chat_rename(request, slug, conversation_id):
    conversation = _conversation(slug, conversation_id)
    title = (request.POST.get('title') or '').strip()
    if title:
        conversation.title = title[:120]
        conversation.save(update_fields=['title', 'updated_at'])
    return redirect('admin_dashboard:ai_employee_chat_thread',
                    slug=slug, conversation_id=conversation.pk)
