"""
The one public, unauthenticated surface: a program's send status.

GoHighLevel sends the messages, so a Pause button in our portal is
decorative unless the workflow asks us whether it may send. This
endpoint is that question.

**The client's GHL workflow must call this as its first step and exit
when ``paused`` is true.** That is a required part of the workflow
build, not an optional nicety — without it, Pause lies to the client.

Keyed on ``ReviewProgram.public_key``, which is unguessable but
deliberately *not* a secret: it grants nothing except "may this program
send right now". It is a separate column from any real secret precisely
so this URL can never be turned into a privileged one.
"""

import logging

from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
@never_cache
def program_status(request, public_key):
    """GET /api/reviews/status/<public_key>/

    ``{"paused": bool, "live": bool, "sms": bool, "email": bool}``

    ``never_cache`` matters: a cached "not paused" would keep messages
    going out after the client pressed Pause, which is the entire
    failure this endpoint exists to prevent.
    """
    from reviews.models import ReviewProgram

    program = (ReviewProgram.objects
               .filter(public_key=public_key)
               .only('setup_status', 'sending_paused', 'reminder_enabled')
               .first())
    if program is None:
        raise Http404('no such program')

    return JsonResponse({
        'paused': bool(program.sending_paused),
        'live': program.setup_status == 'live',
        # Convenience for the workflow's branch conditions, so the
        # reminder step can be switched off without editing the
        # workflow itself.
        'reminder': bool(program.reminder_enabled),
    })
