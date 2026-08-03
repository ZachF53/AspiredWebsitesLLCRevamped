"""Vault background tasks."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def close_idle_ops_sessions():
    """
    Close AI Ops sessions that have gone quiet.

    A session is only closed explicitly when the operator clicks End —
    which does not happen if the tab is closed, the browser crashes, or
    a deploy restarts gunicorn mid-conversation. Those rows sat at
    "LIVE" indefinitely; the sessions list had entries still showing
    live two months after the fact, which makes the whole audit view
    untrustworthy at a glance.

    The list view also calls OpsSession.close_idle() on read, so this
    is a belt to that's braces — staging runs with celerybeat stopped
    on purpose, and the reaping should not depend on a worker being up.
    """
    from .models import OpsSession
    closed = OpsSession.close_idle()
    if closed:
        logger.info('close_idle_ops_sessions: closed %s idle session(s)',
                    closed)
    return closed
