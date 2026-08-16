"""
Wave 7 — optional infrastructure must degrade, not cascade.

Redis backs both the cache and django-ratelimit, and every public form is
rate limited. Before this, an unreachable Redis turned the contact form,
the free audit, login and the scheduler into 500s.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from core.cache_backend import ResilientRedisCache


def _cache():
    return ResilientRedisCache('redis://127.0.0.1:6379', {})


class ResilientCacheTests(TestCase):

    def test_connection_failure_falls_back_instead_of_raising(self):
        cache = _cache()
        with patch(
                'django.core.cache.backends.redis.RedisCache.set',
                side_effect=ConnectionError('redis down')):
            cache.set('resilience-key', 'value')

        self.assertTrue(cache.degraded)
        # The write landed in the fallback and is readable from it.
        with patch(
                'django.core.cache.backends.redis.RedisCache.get',
                side_effect=ConnectionError('redis down')):
            self.assertEqual(cache.get('resilience-key'), 'value')

    def test_ratelimit_counter_keeps_working_while_redis_is_down(self):
        """django-ratelimit counts with add/incr. If those raise, every
        rate-limited view 500s."""
        cache = _cache()
        with patch(
                'django.core.cache.backends.redis.RedisCache.add',
                side_effect=ConnectionError('redis down')), \
             patch(
                'django.core.cache.backends.redis.RedisCache.incr',
                side_effect=ConnectionError('redis down')):
            cache.add('rl-key', 0)
            first = cache.incr('rl-key')
            second = cache.incr('rl-key')

        # Still counting — weaker than Redis, but not absent.
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)

    def test_programming_errors_still_raise(self):
        """Only connection failures are absorbed. A bug must not be
        silently converted into a cache miss."""
        cache = _cache()
        with patch(
                'django.core.cache.backends.redis.RedisCache.get',
                side_effect=TypeError('unhashable key')):
            with self.assertRaises(TypeError):
                cache.get('bad-key')

    def test_recovery_clears_the_degraded_flag(self):
        cache = _cache()
        with patch(
                'django.core.cache.backends.redis.RedisCache.get',
                side_effect=ConnectionError('redis down')):
            cache.get('k')
        self.assertTrue(cache.degraded)

        with patch(
                'django.core.cache.backends.redis.RedisCache.get',
                return_value='back'):
            self.assertEqual(cache.get('k'), 'back')
        self.assertFalse(cache.degraded)

    def test_alerting_is_throttled(self):
        """A Redis outage must not raise one alert per request."""
        cache = _cache()
        with patch('core.system_alerts.record_alert') as alert, \
             patch(
                'django.core.cache.backends.redis.RedisCache.get',
                side_effect=ConnectionError('redis down')):
            for _ in range(25):
                cache.get('k')

        self.assertEqual(alert.call_count, 1)


@override_settings(
    ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
    CACHES={'default': {
        'BACKEND': 'core.cache_backend.ResilientRedisCache',
        'LOCATION': 'redis://127.0.0.1:6379',
    }})
class PublicFormsSurviveRedisOutageTests(TestCase):
    """The end-to-end property that matters: pages that depend on rate
    limiting still serve when Redis is gone."""

    def test_rate_limited_pages_still_render(self):
        for backend_method in ('get', 'set', 'add', 'incr'):
            patch(
                f'django.core.cache.backends.redis.RedisCache.'
                f'{backend_method}',
                side_effect=ConnectionError('redis down')).start()
        self.addCleanup(patch.stopall)

        for path in ('/', '/contact/', '/login/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
