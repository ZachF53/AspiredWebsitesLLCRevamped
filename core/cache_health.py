"""
Is the shared cache actually shared?

``core.cache_backend.ResilientRedisCache`` falls back to a per-process
LocMemCache when Redis is unreachable, which is the right call -- it
keeps the public forms and login alive instead of 500ing them. But the
failure is only reported on the first cache *operation*, which means a
process can boot against a dead Redis and look completely healthy until
a request happens to touch the cache, and the warning then lands in the
middle of request logs rather than at startup.

That matters more now the audit API exists. Its whole latency story is
"a repeat lookup is a cache hit", and with four Gunicorn workers each
holding a private LocMemCache, a hit becomes a one-in-four coin flip
with nothing visibly broken. Rate limiting degrades the same way: it
silently becomes per-worker, so the effective limit is roughly workers x
the configured rate.

So: probe once at startup and say so loudly. The probe is a real
round-trip rather than a connection test, because a write that lands
somewhere unreadable is the failure mode we actually care about.
"""

import logging
import secrets

logger = logging.getLogger(__name__)

PROBE_KEY = 'core:cache_health:probe'

# Results
OK = 'ok'
DEGRADED = 'degraded'
NOT_SHARED = 'not_shared'
SKIPPED = 'skipped'

_probed = False


def probe_cache(force=False):
    """
    Round-trip the default cache once.

    Returns one of:
      ``ok``          Redis answered and the value came back intact.
      ``degraded``    the backend fell back to its per-process cache.
      ``not_shared``  the write did not read back -- treated as degraded.
      ``skipped``     not a resilient-Redis backend (tests, rehearsal).

    Never raises. A health check that can break startup is worse than
    the condition it reports.
    """
    global _probed
    if _probed and not force:
        return SKIPPED
    _probed = True

    try:
        from django.core.cache import cache

        # LocMemCache under settings_test / settings_rehearsal has no
        # Redis to lose, so there is nothing to report.
        if not hasattr(cache, 'degraded'):
            return SKIPPED

        token = secrets.token_hex(8)
        cache.set(PROBE_KEY, token, 30)
        echoed = cache.get(PROBE_KEY)

        if getattr(cache, 'degraded', False):
            logger.error(
                'STARTUP: Redis is NOT reachable. The cache and '
                'django-ratelimit have degraded to a per-process '
                'fallback. Audit-API cache hits will be unreliable '
                'across workers and rate limits are now per worker '
                'rather than global. Check: redis-cli -u "$REDIS_URL" ping'
            )
            return DEGRADED

        if echoed != token:
            logger.error(
                'STARTUP: the cache accepted a write but read back %r '
                'instead of the probe value. The cache is not behaving '
                'as a shared store; treat it as degraded.', echoed,
            )
            return NOT_SHARED

        logger.info('STARTUP: Redis cache reachable (round-trip ok)')
        return OK
    except Exception:
        # Including this path: a probe that explodes must not stop a
        # worker from booting.
        logger.exception('STARTUP: cache health probe failed to run')
        return DEGRADED
