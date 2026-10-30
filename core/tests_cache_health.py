"""
Startup cache probe.

The condition under test is the one that produces no error at all: Redis
is gone, ResilientRedisCache quietly serves a per-process LocMemCache,
and every page still works. The audit API's latency story depends on
cache hits being shared across workers, so "degraded but silent" has to
become "degraded and loud".
"""

from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from core import cache_health
from core.cache_health import DEGRADED, NOT_SHARED, OK, SKIPPED, probe_cache


class _FakeCache:
    """Stands in for ResilientRedisCache. Has a ``degraded`` flag, which
    is what the probe uses to recognise a resilient backend."""

    def __init__(self, degraded=False, echo=True):
        self.degraded = degraded
        self._echo = echo
        self._store = {}

    def set(self, key, value, timeout=None):
        if self._echo:
            self._store[key] = value

    def get(self, key, default=None):
        return self._store.get(key, default)


class _PlainCache:
    """LocMemCache-alike: no ``degraded`` attribute at all."""

    def set(self, *a, **k):
        pass

    def get(self, *a, **k):
        return None


class ProbeCacheTests(SimpleTestCase):

    def setUp(self):
        cache_health._probed = False

    def test_healthy_redis_reports_ok(self):
        with patch('django.core.cache.cache', _FakeCache()):
            self.assertEqual(probe_cache(force=True), OK)

    def test_degraded_backend_reports_degraded_and_logs_error(self):
        with patch('django.core.cache.cache', _FakeCache(degraded=True)):
            with self.assertLogs('core.cache_health', 'ERROR') as logs:
                self.assertEqual(probe_cache(force=True), DEGRADED)

        joined = '\n'.join(logs.output)
        self.assertIn('Redis is NOT reachable', joined)
        # The message has to tell whoever reads it what broke, not just
        # that something did.
        self.assertIn('per-process', joined)
        self.assertIn('rate limit', joined.lower())
        self.assertIn('redis-cli', joined)

    def test_write_that_does_not_read_back_is_not_shared(self):
        """A cache that accepts writes and loses them is degraded even
        though it never raised and never set the flag."""
        with patch('django.core.cache.cache', _FakeCache(echo=False)):
            with self.assertLogs('core.cache_health', 'ERROR'):
                self.assertEqual(probe_cache(force=True), NOT_SHARED)

    def test_non_resilient_backend_is_skipped_silently(self):
        """LocMemCache under settings_test has no Redis to lose."""
        with patch('django.core.cache.cache', _PlainCache()):
            self.assertEqual(probe_cache(force=True), SKIPPED)

    def test_probe_runs_once_per_process_unless_forced(self):
        with patch('django.core.cache.cache', _FakeCache()):
            self.assertEqual(probe_cache(), OK)
            self.assertEqual(probe_cache(), SKIPPED)
            self.assertEqual(probe_cache(force=True), OK)

    def test_probe_never_raises(self):
        """A health check that can break startup is worse than the
        condition it reports."""
        boom = patch('django.core.cache.cache')
        with boom as fake:
            type(fake).degraded = False
            fake.set.side_effect = RuntimeError('redis exploded')
            with self.assertLogs('core.cache_health', 'ERROR'):
                self.assertEqual(probe_cache(force=True), DEGRADED)


class CheckRedisCommandTests(SimpleTestCase):

    def setUp(self):
        cache_health._probed = False

    def test_command_raises_on_degraded_so_it_can_gate_a_deploy(self):
        with patch('django.core.cache.cache', _FakeCache(degraded=True)):
            with self.assertRaises(CommandError) as ctx:
                call_command('check_redis')
        self.assertIn('NOT serving', str(ctx.exception))

    def test_command_succeeds_on_healthy_redis(self):
        with patch('django.core.cache.cache', _FakeCache()):
            # _report_clients reaches for cache._cache.get_client(), which
            # the fake has no answer for; the command must survive that.
            call_command('check_redis')

    @override_settings(CACHES={'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'x',
    }})
    def test_command_warns_rather_than_fails_when_not_redis(self):
        with patch('django.core.cache.cache', _PlainCache()):
            call_command('check_redis')

    def test_password_is_masked_in_the_reported_location(self):
        from core.management.commands.check_redis import _safe_location
        with override_settings(CACHES={'default': {
            'BACKEND': 'core.cache_backend.ResilientRedisCache',
            'LOCATION': 'redis://default:sup3rsecret@10.0.0.5:6379/0',
        }}):
            shown = _safe_location()
        self.assertNotIn('sup3rsecret', shown)
        self.assertIn('10.0.0.5', shown)
        self.assertIn('***', shown)
