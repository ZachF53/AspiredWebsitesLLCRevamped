"""
The one public, unauthenticated surface: may the agent answer right now?

GoHighLevel answers the calls, so a Pause button in our portal is
decorative unless the agent asks us first. This endpoint is that
question, and it is the direct analogue of
``reviews/views_public.program_status``.

**The client's GHL Voice AI agent must call this as a custom action at
the start of a call and hang up politely when ``answer`` is false.** That
is a required part of the agent build, not an optional nicety — without
it, Pause lies to the client.

It also returns ``within_hours``, so a single agent can carry an
after-hours schedule without the schedule having to be mirrored into
GHL's working-hours UI. That matters because GHL's working hours have no
timezone field of their own: we know the client's zone, GHL does not, so
letting us answer the question is strictly more reliable than pushing
times and hoping the sub-account zone matches.

Keyed on ``VoiceProgram.public_key`` — unguessable but deliberately *not*
a secret. It grants nothing except "may this agent answer", and it is a
separate column from the real credentials precisely so this URL can never
be turned into a privileged one.
"""

import logging

from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
@never_cache
def program_status(request, public_key):
    """GET /api/voice/status/<public_key>/

    ``{"answer": bool, "live": bool, "paused": bool,
       "within_hours": bool, "coverage": str, "answer_mode": str,
       "ring_seconds": int, "record": bool}``

    ``never_cache`` matters: a cached "answer: true" would keep the agent
    picking up after the client pressed Pause, which is the whole failure
    this endpoint exists to prevent.
    """
    from voice.models import VoiceProgram

    program = (VoiceProgram.objects
               .filter(public_key=public_key)
               .prefetch_related('hours')
               .first())
    if program is None:
        raise Http404('no such program')

    now = timezone.now()
    within_hours = True
    try:
        from voice.schedule import is_after_hours
        within_hours = not is_after_hours(program, now)
    except Exception:
        # Fail toward answering. A schedule bug that silenced the
        # receptionist would cost the client jobs; one that answers an
        # in-hours call the owner would also have taken costs a few
        # cents of talk time.
        logger.exception('voice.status: hours check failed for %s',
                         program.pk)

    coverage = program.coverage_mode
    if coverage == 'all_hours':
        in_window = True
    elif coverage == 'after_hours_only':
        in_window = not within_hours
    elif coverage == 'business_hours_only':
        in_window = within_hours
    else:  # 'custom' — stored windows describe coverage directly
        in_window = not within_hours

    should_answer = (program.setup_status == 'live'
                     and not program.paused
                     and in_window)

    return JsonResponse({
        'answer': should_answer,
        'live': program.setup_status == 'live',
        'paused': bool(program.paused),
        'within_hours': within_hours,
        'coverage': coverage,
        # Lets one agent handle both answer modes without a rebuild.
        'answer_mode': program.answer_mode,
        'ring_seconds': (program.ring_seconds
                         if program.answer_mode == 'ring_you_first' else 0),
        'transfer_number': program.transfer_number,
        'record': bool(program.recording_enabled),
        # Spam screening is only possible when the agent answers first —
        # in ring-you-first the robocall has already reached the owner's
        # phone. Returned so the agent prompt does not promise screening
        # it cannot perform.
        'screen_spam': program.screens_spam,
    })
