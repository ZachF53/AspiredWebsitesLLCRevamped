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
from django.views.decorators.http import require_POST

from admin_dashboard.context import _admin_context
from admin_dashboard.decorators import admin_required

logger = logging.getLogger(__name__)
from admin_dashboard.models import (
    AIEmployee,
    AIEmployeeAction,
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
