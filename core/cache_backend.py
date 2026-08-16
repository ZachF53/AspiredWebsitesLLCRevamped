"""
A Redis cache that survives Redis being unavailable.

`django.core.cache.backends.redis.RedisCache` propagates connection errors.
That is reasonable for a cache used as a cache — but this one is also the
store behind `django-ratelimit`, and every public form is rate limited:
contact, the free audit, login, and the scheduler. So a Redis outage does
not degrade the site, it takes the forms down with a 500. A dependency that
exists to protect the site should not be able to break it.

This backend catches only *connection* failures — the error classes that
mean "Redis is not reachable right now" — and falls back to a per-process
local cache for the duration. Programming errors, serialization failures
and bad keys still raise, because those are bugs and silencing them would
hide real defects.

Falling back to a local cache rather than failing open is deliberate. With
several Gunicorn workers, a per-process counter means the effective rate
limit is roughly workers x the configured rate: weaker than Redis, far
better than none. The alternative — treating every check as "not limited" —
would leave login and the public forms completely unprotected during
exactly the kind of incident when that matters.

The first failure raises a system alert so the degradation is visible
rather than silent; subsequent failures inside the cooldown do not, to
avoid a Redis outage generating an alert per request.
"""

import logging
import time

from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache

logger = logging.getLogger(__name__)


def _connection_errors():
    """Error classes meaning Redis is unreachable, resolved lazily."""
    errors = [ConnectionError, TimeoutError, OSError]
    try:
        from redis import exceptions as redis_exceptions

        errors.extend([
            redis_exceptions.ConnectionError,
            redis_exceptions.TimeoutError,
            redis_exceptions.BusyLoadingError,
        ])
    except Exception:
        pass
    return tuple(errors)


# Seconds between alerts while Redis stays down.
ALERT_COOLDOWN_SECONDS = 300


class ResilientRedisCache(RedisCache):
    """RedisCache that degrades to a local cache when Redis is down."""

    def __init__(self, server, params):
        super().__init__(server, params)
        self._fallback = LocMemCache(
            'aspired-redis-fallback', {'OPTIONS': {'MAX_ENTRIES': 5000}})
        self._errors = _connection_errors()
        self._last_alert_at = 0.0
        self.degraded = False

    # ── failure handling ────────────────────────────────────────────

    def _note_failure(self, operation, exc):
        self.degraded = True
        logger.warning(
            'cache: Redis unavailable during %s (%s) — using the '
            'per-process fallback', operation, exc.__class__.__name__)

        now = time.monotonic()
        if now - self._last_alert_at < ALERT_COOLDOWN_SECONDS:
            return
        self._last_alert_at = now
        try:
            from core.system_alerts import record_alert

            record_alert(
                severity='error',
                source='core.cache.redis_unavailable',
                message='Redis is unreachable; cache and rate limiting '
                        'have degraded to a per-process fallback.',
                detail=f'{exc.__class__.__name__} during {operation}. Rate '
                       'limits are now per worker rather than global.',
            )
        except Exception:
            logger.exception('cache: could not record the Redis alert')

    def _call(self, name, *args, **kwargs):
        """Run a Redis operation, falling back on connection failure."""
        try:
            result = getattr(super(), name)(*args, **kwargs)
        except self._errors as exc:
            self._note_failure(name, exc)
            return getattr(self._fallback, name)(*args, **kwargs)
        else:
            if self.degraded:
                logger.info('cache: Redis is reachable again')
                self.degraded = False
            return result

    # ── the operations django-ratelimit and the app actually use ────

    def add(self, *args, **kwargs):
        return self._call('add', *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._call('get', *args, **kwargs)

    def set(self, *args, **kwargs):
        return self._call('set', *args, **kwargs)

    def touch(self, *args, **kwargs):
        return self._call('touch', *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._call('delete', *args, **kwargs)

    def get_many(self, *args, **kwargs):
        return self._call('get_many', *args, **kwargs)

    def set_many(self, *args, **kwargs):
        return self._call('set_many', *args, **kwargs)

    def delete_many(self, *args, **kwargs):
        return self._call('delete_many', *args, **kwargs)

    def has_key(self, *args, **kwargs):
        return self._call('has_key', *args, **kwargs)

    def incr(self, *args, **kwargs):
        # django-ratelimit's counter. A missing key raises ValueError,
        # which is meaningful and must not be swallowed.
        return self._call('incr', *args, **kwargs)

    def decr(self, *args, **kwargs):
        return self._call('decr', *args, **kwargs)

    def clear(self, *args, **kwargs):
        return self._call('clear', *args, **kwargs)
