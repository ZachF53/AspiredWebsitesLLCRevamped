"""
Audit machine API — the surface a GHL Voice AI agent calls.

Convention follows public/tests.py: every external call is mocked, no
live PageSpeed and no live callback POST.

The test that matters most is
``AuditValidityGuardTests.test_404_page_scoring_100_is_invalid_url``.
PSI returns HTTP 200 with a performance score of 100 for a 404 page,
because a lightweight error page really is fast. Measured and confirmed
against the live API. If that ever reaches a caller as "your site scores
100", the feature is worse than not existing, so the guard is pinned
here rather than trusted to stay correct.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from public import views_audit_api
from public.tasks import (
    AUDIT_FAILURE_TTL, AUDIT_SUCCESS_TTL, audit_cache_key, job_cache_key,
    run_audit_for_voice,
)
from public.views import _PageSpeedError, normalized_audit_url

SECRET = 'audit-api-test-secret'


# ── Fixtures ────────────────────────────────────────────────────────────────

def _raw(requested='https://example.com/', final='https://example.com/',
         lcp_ms=4952, crux='SLOW'):
    """A minimal but structurally real PSI payload."""
    return {
        'lighthouseResult': {
            'requestedUrl': requested,
            'finalUrl': final,
            'categories': {
                'performance': {'score': 0.72},
                'seo': {'score': 1.0},
                'best-practices': {'score': 0.96},
                'accessibility': {'score': 0.84},
            },
            'audits': {
                'largest-contentful-paint': {
                    'numericValue': lcp_ms,
                    'displayValue': '5.0\xa0s',
                },
            },
        },
        'loadingExperience': {'overall_category': crux},
    }


def _psi_result(raw=None, performance=72):
    """Shape matches _run_pagespeed_audit(include_raw=True)."""
    return {
        'scores': {
            'performance': performance,
            'seo': 100,
            'best_practices': 96,
            'accessibility': 84,
        },
        'issues_by_category': {
            'performance': [], 'seo': [],
            'best_practices': [], 'accessibility': [],
        },
        'raw': raw if raw is not None else _raw(),
    }


class _Resp:
    """Stand-in for a requests Response from the verification probe."""

    def __init__(self, status_code, url):
        self.status_code = status_code
        self.url = url

    def close(self):
        pass


# ── Auth ───────────────────────────────────────────────────────────────────

class AuditApiAuthTests(TestCase):
    """The secret gate, including the fail-closed case."""

    def setUp(self):
        cache.clear()

    def _post(self, **headers):
        return self.client.post(
            reverse('public:audit_api_request'),
            data={'url': 'https://example.com'},
            **headers,
        )

    @override_settings(AUDIT_API_SECRET=SECRET)
    def test_missing_secret_header_is_403(self):
        self.assertEqual(self._post().status_code, 403)

    @override_settings(AUDIT_API_SECRET=SECRET)
    def test_wrong_secret_is_403(self):
        r = self._post(HTTP_X_AUDIT_SECRET='not-the-secret')
        self.assertEqual(r.status_code, 403)

    @override_settings(AUDIT_API_SECRET='')
    def test_unset_secret_fails_closed(self):
        """An empty AUDIT_API_SECRET must reject everything, including a
        request that sends an empty header and would otherwise compare
        equal."""
        self.assertEqual(self._post(HTTP_X_AUDIT_SECRET='').status_code, 403)
        self.assertEqual(
            self._post(HTTP_X_AUDIT_SECRET='anything').status_code, 403)

    @override_settings(AUDIT_API_SECRET=SECRET)
    def test_get_is_405(self):
        r = self.client.get(reverse('public:audit_api_request'),
                            HTTP_X_AUDIT_SECRET=SECRET)
        self.assertEqual(r.status_code, 405)

    @override_settings(AUDIT_API_SECRET=SECRET)
    def test_status_endpoint_requires_secret(self):
        r = self.client.get(
            reverse('public:audit_api_status', args=['job-1']))
        self.assertEqual(r.status_code, 403)


# ── Request endpoint: cache miss / hit ─────────────────────────────────────

@override_settings(AUDIT_API_SECRET=SECRET, GHL_AUDIT_WEBHOOK_URL='')
class AuditRequestTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_cache_miss_enqueues_and_returns_202(self):
        with patch.object(views_audit_api, 'run_audit_for_voice') as task:
            task.delay.return_value = SimpleNamespace(id='job-abc')
            r = self.client.post(
                reverse('public:audit_api_request'),
                data={'url': 'https://example.com', 'contact_id': 'c1',
                      'phone': '+12102010375'},
                HTTP_X_AUDIT_SECRET=SECRET,
            )

        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual(body['status'], 'pending')
        self.assertEqual(body['job_id'], 'job-abc')
        task.delay.assert_called_once_with(
            'https://example.com', contact_id='c1', phone='+12102010375')

    def test_cache_hit_returns_200_with_cached_true(self):
        payload = {
            'status': 'ready',
            'url': 'https://example.com',
            'final_url': 'https://example.com/',
            'performance_score': 72,
            'lcp_ms': 4952,
            'lcp_seconds': 5.0,
            'crux': 'SLOW',
            'summary': 'example.com has 2 areas that need attention.',
        }
        cache.set(audit_cache_key('https://example.com'), payload, 60)

        with patch.object(views_audit_api, 'run_audit_for_voice') as task:
            r = self.client.post(
                reverse('public:audit_api_request'),
                data={'url': 'https://example.com'},
                HTTP_X_AUDIT_SECRET=SECRET,
            )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['cached'])
        self.assertEqual(body['status'], 'ready')
        self.assertEqual(body['performance_score'], 72)
        task.delay.assert_not_called()

    def test_json_body_is_accepted(self):
        """GHL posts JSON, not form data."""
        with patch.object(views_audit_api, 'run_audit_for_voice') as task:
            task.delay.return_value = SimpleNamespace(id='job-json')
            r = self.client.post(
                reverse('public:audit_api_request'),
                data='{"url": "https://example.com", "phone": "+1210"}',
                content_type='application/json',
                HTTP_X_AUDIT_SECRET=SECRET,
            )
        self.assertEqual(r.status_code, 202)
        task.delay.assert_called_once_with(
            'https://example.com', contact_id=None, phone='+1210')

    def test_missing_url_is_400(self):
        r = self.client.post(reverse('public:audit_api_request'), data={},
                             HTTP_X_AUDIT_SECRET=SECRET)
        self.assertEqual(r.status_code, 400)

    def test_unparseable_url_is_400(self):
        r = self.client.post(reverse('public:audit_api_request'),
                             data={'url': 'not a url at all'},
                             HTTP_X_AUDIT_SECRET=SECRET)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['status'], 'failed')


# ── The validity guard ─────────────────────────────────────────────────────

@override_settings(AUDIT_API_SECRET=SECRET, GHL_AUDIT_WEBHOOK_URL='')
class AuditValidityGuardTests(TestCase):
    """Everything that decides whether a PSI result can be trusted."""

    def setUp(self):
        cache.clear()

    def test_404_page_scoring_100_is_invalid_url(self):
        """*** The one that matters. ***

        A 404 path on a live domain: PSI returns 200 and a performance
        score of 100. The independent probe sees the real 404, so the
        result must come back as invalid_url and must NOT carry a score.
        """
        url = 'https://example.com/definitely-not-a-page-xyz'
        raw = _raw(requested=url, final=url)

        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw, performance=100)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(404, url)):
            payload = run_audit_for_voice(url)

        self.assertEqual(payload['status'], 'invalid_url')
        self.assertEqual(payload['http_status'], 404)
        self.assertNotIn('performance_score', payload)
        self.assertNotIn('100', payload['reason'])
        self.assertIn('double-check', payload['reason'].lower())

    def test_http_to_https_redirect_is_accepted(self):
        """A scheme upgrade is normal and must not trip the guard."""
        raw = _raw(requested='http://example.com/',
                   final='https://example.com/')

        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')):
            payload = run_audit_for_voice('http://example.com')

        self.assertEqual(payload['status'], 'ready')
        self.assertEqual(payload['performance_score'], 72)
        self.assertEqual(payload['lcp_ms'], 4952)
        self.assertEqual(payload['lcp_seconds'], 5.0)
        self.assertEqual(payload['crux'], 'SLOW')
        self.assertEqual(payload['summary'],
                         'example.com has 2 areas that need attention.')

    def test_www_and_trailing_slash_change_is_accepted(self):
        raw = _raw(requested='https://www.example.com',
                   final='https://example.com/')
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')):
            payload = run_audit_for_voice('https://www.example.com')
        self.assertEqual(payload['status'], 'ready')

    def test_cross_domain_redirect_returns_redirected(self):
        raw = _raw(requested='https://example.com/',
                   final='https://somewhereelse.net/')

        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://somewhereelse.net/')):
            payload = run_audit_for_voice('https://example.com')

        self.assertEqual(payload['status'], 'redirected')
        self.assertEqual(payload['final_url'], 'https://somewhereelse.net/')
        self.assertEqual(payload['requested_url'], 'https://example.com/')
        self.assertIn('somewhereelse.net', payload['reason'])
        # The scores belong to the other site — never report them as the
        # caller's.
        self.assertNotIn('performance_score', payload)

    def test_subdomain_change_is_not_a_cross_domain_redirect(self):
        """shop.example.com shares a registrable domain with example.com."""
        raw = _raw(requested='https://example.com/',
                   final='https://shop.example.com/')
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://shop.example.com/')):
            payload = run_audit_for_voice('https://example.com')
        self.assertEqual(payload['status'], 'ready')

    def test_site_that_will_not_load_is_invalid_url(self):
        """The probe cannot complete at all — not a result."""
        import requests as _requests
        raw = _raw()
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   side_effect=_requests.ConnectionError('boom')), \
             patch('public.tasks.requests.get',
                   side_effect=_requests.ConnectionError('boom')):
            payload = run_audit_for_voice('https://example.com')

        self.assertEqual(payload['status'], 'invalid_url')
        self.assertNotIn('performance_score', payload)

    def test_head_405_falls_back_to_get(self):
        """Hosts that refuse HEAD but serve GET are not dead sites."""
        raw = _raw()
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(405, 'https://example.com/')), \
             patch('public.tasks.requests.get',
                   return_value=_Resp(200, 'https://example.com/')) as get:
            payload = run_audit_for_voice('https://example.com')

        self.assertEqual(payload['status'], 'ready')
        get.assert_called_once()


# ── PSI failure ────────────────────────────────────────────────────────────

@override_settings(AUDIT_API_SECRET=SECRET, GHL_AUDIT_WEBHOOK_URL='')
class AuditFailureTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_psi_400_returns_failed_with_readable_reason(self):
        """FAILED_DOCUMENT_REQUEST — a real site PSI cannot load."""
        err = _PageSpeedError('web copy, calls a phone number',
                              code='bad_status')
        with patch('public.views._run_pagespeed_audit', side_effect=err):
            payload = run_audit_for_voice('https://example.com')

        self.assertEqual(payload['status'], 'failed')
        reason = payload['reason']
        # Readable aloud: no phone number, no "open a browser", no URL.
        self.assertNotIn('210-201-0375', reason)
        self.assertNotIn('browser', reason.lower())
        self.assertIn('could not analyze', reason.lower())
        self.assertNotIn('performance_score', payload)

    def test_psi_timeout_reason_differs_from_unreachable(self):
        """The voice phrasing branches on the error code, so the two must
        not collapse into one message."""
        with patch('public.views._run_pagespeed_audit',
                   side_effect=_PageSpeedError('x', code='timeout')):
            timed_out = run_audit_for_voice('https://a-example.com')
        with patch('public.views._run_pagespeed_audit',
                   side_effect=_PageSpeedError('x', code='bad_status')):
            bad_status = run_audit_for_voice('https://b-example.com')

        self.assertNotEqual(timed_out['reason'], bad_status['reason'])

    def test_failure_is_cached_with_the_shorter_ttl(self):
        url = 'https://example.com'
        with patch('public.views._run_pagespeed_audit',
                   side_effect=_PageSpeedError('x', code='bad_status')), \
             patch('public.tasks.cache.set') as cache_set:
            run_audit_for_voice(url)

        ttls = {
            call.args[0]: call.args[2]
            for call in cache_set.call_args_list if len(call.args) >= 3
        }
        self.assertEqual(ttls[audit_cache_key(url)], AUDIT_FAILURE_TTL)
        self.assertNotEqual(ttls[audit_cache_key(url)], AUDIT_SUCCESS_TTL)
        self.assertEqual(AUDIT_FAILURE_TTL, 60 * 60)

    def test_success_is_cached_with_the_longer_ttl(self):
        url = 'https://example.com'
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result()), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')), \
             patch('public.tasks.cache.set') as cache_set:
            run_audit_for_voice(url)

        ttls = {
            call.args[0]: call.args[2]
            for call in cache_set.call_args_list if len(call.args) >= 3
        }
        self.assertEqual(ttls[audit_cache_key(url)], AUDIT_SUCCESS_TTL)
        self.assertEqual(AUDIT_SUCCESS_TTL, 60 * 60 * 24)


# ── Callback ───────────────────────────────────────────────────────────────

class AuditCallbackTests(TestCase):

    def setUp(self):
        cache.clear()

    @override_settings(GHL_AUDIT_WEBHOOK_URL='')
    def test_empty_webhook_url_skips_without_crashing(self):
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result()), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')), \
             patch('public.tasks.requests.post') as post:
            payload = run_audit_for_voice('https://example.com')

        self.assertEqual(payload['status'], 'ready')
        post.assert_not_called()

    @override_settings(GHL_AUDIT_WEBHOOK_URL='https://hooks.example/audit')
    def test_callback_includes_contact_id_and_phone(self):
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result()), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')), \
             patch('public.tasks.requests.post') as post:
            post.return_value = SimpleNamespace(status_code=200)
            run_audit_for_voice('https://example.com',
                                contact_id='c99', phone='+12102010375')

        post.assert_called_once()
        body = post.call_args.kwargs['json']
        self.assertEqual(body['contact_id'], 'c99')
        self.assertEqual(body['phone'], '+12102010375')
        self.assertEqual(body['status'], 'ready')
        self.assertEqual(body['performance_score'], 72)

    @override_settings(GHL_AUDIT_WEBHOOK_URL='https://hooks.example/audit')
    def test_callback_failure_does_not_break_the_audit(self):
        import requests as _requests
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result()), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')), \
             patch('public.tasks.requests.post',
                   side_effect=_requests.ConnectionError('down')):
            payload = run_audit_for_voice('https://example.com')

        self.assertEqual(payload['status'], 'ready')


# ── Status endpoint ────────────────────────────────────────────────────────

@override_settings(AUDIT_API_SECRET=SECRET)
class AuditStatusTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_unknown_job_is_404(self):
        r = self.client.get(
            reverse('public:audit_api_status', args=['nope']),
            HTTP_X_AUDIT_SECRET=SECRET)
        self.assertEqual(r.status_code, 404)

    def test_pending_job_reports_pending(self):
        cache.set(job_cache_key('job-1'),
                  {'status': 'pending', 'url': 'https://example.com'}, 60)
        r = self.client.get(
            reverse('public:audit_api_status', args=['job-1']),
            HTTP_X_AUDIT_SECRET=SECRET)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'pending')

    def test_finished_job_reports_payload(self):
        cache.set(job_cache_key('job-2'), {
            'status': 'ready',
            'url': 'https://example.com',
            'performance_score': 72,
            'summary': 'example.com has 2 areas that need attention.',
        }, 60)
        r = self.client.get(
            reverse('public:audit_api_status', args=['job-2']),
            HTTP_X_AUDIT_SECRET=SECRET)
        body = r.json()
        self.assertEqual(body['status'], 'ready')
        self.assertEqual(body['performance_score'], 72)

    def test_failed_job_reports_reason(self):
        cache.set(job_cache_key('job-3'),
                  {'status': 'failed', 'reason': 'I could not analyze that.'},
                  60)
        r = self.client.get(
            reverse('public:audit_api_status', args=['job-3']),
            HTTP_X_AUDIT_SECRET=SECRET)
        self.assertEqual(r.json()['reason'], 'I could not analyze that.')


# ── Rate limiting ──────────────────────────────────────────────────────────

class AuditRateLimitKeyTests(TestCase):
    """The bucket must be the URL, never the caller's IP — every request
    arrives from one GoHighLevel address."""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def _key(self, url, ip):
        request = self.factory.post(
            '/api/audit/request/',
            data={'url': url},
            REMOTE_ADDR=ip,
        )
        return views_audit_api._ratelimit_url_key('g', request)

    def test_same_url_different_ips_share_one_bucket(self):
        a = self._key('https://example.com', '203.0.113.1')
        b = self._key('https://example.com', '198.51.100.9')
        self.assertEqual(a, b)

    def test_different_urls_get_different_buckets(self):
        a = self._key('https://example.com', '203.0.113.1')
        b = self._key('https://other-example.com', '203.0.113.1')
        self.assertNotEqual(a, b)

    def test_key_does_not_contain_the_ip(self):
        key = self._key('https://example.com', '203.0.113.1')
        self.assertNotIn('203.0.113.1', key)
        self.assertIn('example.com', key)

    def test_key_survives_a_json_body(self):
        """'post:url' would return '' here, collapsing every JSON request
        into one shared bucket. The callable must not."""
        request = self.factory.post(
            '/api/audit/request/',
            data='{"url": "https://example.com"}',
            content_type='application/json',
            REMOTE_ADDR='203.0.113.1',
        )
        key = views_audit_api._ratelimit_url_key('g', request)
        self.assertIn('example.com', key)


# ── Logging ────────────────────────────────────────────────────────────────

@override_settings(AUDIT_API_SECRET=SECRET, GHL_AUDIT_WEBHOOK_URL='')
class AuditLoggingTests(TestCase):
    """Every request logs raw URL + normalized key + hit/miss + status;
    every guard rejection logs requestedUrl, finalUrl and the probe's
    HTTP status."""

    def setUp(self):
        cache.clear()

    def test_cache_miss_logs_raw_key_miss_and_status(self):
        with patch.object(views_audit_api, 'run_audit_for_voice') as task:
            task.delay.return_value = SimpleNamespace(id='job-log')
            with self.assertLogs('public.views_audit_api', 'INFO') as logs:
                self.client.post(
                    reverse('public:audit_api_request'),
                    data={'url': 'https://www.Example.com/'},
                    HTTP_X_AUDIT_SECRET=SECRET,
                )

        line = next(m for m in logs.output if 'raw=' in m)
        self.assertIn("raw='https://www.Example.com/'", line)
        self.assertIn(f'key={audit_cache_key("https://www.example.com")}',
                      line)
        self.assertIn('cache=miss', line)
        self.assertIn('status=pending', line)
        self.assertIn('http=202', line)

    def test_cache_hit_logs_hit_and_ready(self):
        cache.set(audit_cache_key('https://example.com'),
                  {'status': 'ready', 'performance_score': 72}, 60)
        with self.assertLogs('public.views_audit_api', 'INFO') as logs:
            self.client.post(
                reverse('public:audit_api_request'),
                data={'url': 'https://example.com'},
                HTTP_X_AUDIT_SECRET=SECRET,
            )
        line = next(m for m in logs.output if 'raw=' in m)
        self.assertIn('cache=hit', line)
        self.assertIn('status=ready', line)
        self.assertIn('http=200', line)

    def test_rejected_request_is_logged_with_its_status(self):
        with self.assertLogs('public.views_audit_api', 'INFO') as logs:
            self.client.post(
                reverse('public:audit_api_request'),
                data={'url': 'not a url at all'},
                HTTP_X_AUDIT_SECRET=SECRET,
            )
        line = next(m for m in logs.output if 'raw=' in m)
        self.assertIn("raw='not a url at all'", line)
        self.assertIn('status=unparseable_url', line)
        self.assertIn('http=400', line)

    def test_unauthorized_logs_without_leaking_the_secret(self):
        with self.assertLogs('public.views_audit_api', 'INFO') as logs:
            self.client.post(
                reverse('public:audit_api_request'),
                data={'url': 'https://example.com'},
                HTTP_X_AUDIT_SECRET='wrong-secret-value',
            )
        line = next(m for m in logs.output if 'raw=' in m)
        self.assertIn('status=unauthorized', line)
        self.assertNotIn('wrong-secret-value', line)
        self.assertNotIn(SECRET, line)

    def test_404_guard_rejection_logs_both_urls_and_the_probe_status(self):
        url = 'https://example.com/definitely-not-a-page-xyz'
        raw = _raw(requested=url, final=url)
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw, performance=100)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(404, url)):
            with self.assertLogs('public.tasks', 'WARNING') as logs:
                run_audit_for_voice(url)

        line = next(m for m in logs.output if 'REJECTED' in m)
        self.assertIn('invalid_url', line)
        self.assertIn(f'requestedUrl={url}', line)
        self.assertIn(f'finalUrl={url}', line)
        self.assertIn('probe_http=404', line)

    def test_cross_domain_guard_rejection_logs_both_urls(self):
        raw = _raw(requested='https://example.com/',
                   final='https://somewhereelse.net/')
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://somewhereelse.net/')):
            with self.assertLogs('public.tasks', 'WARNING') as logs:
                run_audit_for_voice('https://example.com')

        line = next(m for m in logs.output if 'REJECTED' in m)
        self.assertIn('redirected', line)
        self.assertIn('requestedUrl=https://example.com/', line)
        self.assertIn('finalUrl=https://somewhereelse.net/', line)
        self.assertIn('probe_http=200', line)

    def test_unreachable_guard_rejection_is_logged(self):
        import requests as _requests
        raw = _raw()
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   side_effect=_requests.ConnectionError('boom')), \
             patch('public.tasks.requests.get',
                   side_effect=_requests.ConnectionError('boom')):
            with self.assertLogs('public.tasks', 'WARNING') as logs:
                run_audit_for_voice('https://example.com')

        line = next(m for m in logs.output if 'REJECTED' in m)
        self.assertIn('probe_http=unreachable', line)

    def test_accepted_audit_logs_no_rejection(self):
        raw = _raw()
        with patch('public.views._run_pagespeed_audit',
                   return_value=_psi_result(raw=raw)), \
             patch('public.tasks.requests.head',
                   return_value=_Resp(200, 'https://example.com/')):
            with self.assertLogs('public.tasks', 'INFO') as logs:
                run_audit_for_voice('https://example.com')

        self.assertFalse([m for m in logs.output if 'REJECTED' in m])
        self.assertTrue([m for m in logs.output if 'http=200' in m])


# ── URL normalisation ──────────────────────────────────────────────────────

class NormalizedAuditUrlTests(TestCase):
    """Cache-key canonicalisation. http/https must collide; distinct
    paths must not."""

    def test_scheme_is_dropped_so_http_and_https_collide(self):
        self.assertEqual(normalized_audit_url('http://example.com'),
                         normalized_audit_url('https://example.com'))

    def test_www_and_case_and_trailing_slash_are_stripped(self):
        self.assertEqual(normalized_audit_url('https://WWW.Example.com/'),
                         'example.com')

    def test_query_and_fragment_are_dropped(self):
        self.assertEqual(
            normalized_audit_url('https://example.com/a?utm=x#frag'),
            'example.com/a')

    def test_distinct_paths_do_not_collide(self):
        self.assertNotEqual(normalized_audit_url('https://example.com/a'),
                            normalized_audit_url('https://example.com/b'))

    def test_bare_domain_is_accepted(self):
        self.assertEqual(normalized_audit_url('example.com'), 'example.com')

    def test_garbage_returns_empty(self):
        self.assertEqual(normalized_audit_url(''), '')
        self.assertEqual(normalized_audit_url('   '), '')

    def test_phrases_and_bare_words_are_rejected(self):
        """urlparse('https://not a url at all').hostname returns the whole
        phrase. Rejecting it here is what keeps a spoken non-answer from
        becoming a cache key and a 15-second worker job."""
        for bad in ('not a url at all', 'hello', 'localhost',
                    'https://no spaces allowed.com', 'https://-lead.com',
                    'https://trail-.com', 'https://.com'):
            with self.subTest(bad=bad):
                self.assertEqual(normalized_audit_url(bad), '')

    def test_cache_key_is_safe_and_bounded(self):
        """Keys must carry no whitespace and stay well inside Django's
        250-character warning, whatever the path looked like."""
        key = audit_cache_key('https://example.com/' + 'deep/' * 80)
        self.assertTrue(key.startswith('audit_api:'))
        self.assertLess(len(key), 60)
        self.assertFalse(any(c.isspace() for c in key))

    def test_cache_key_is_empty_for_a_rejected_url(self):
        self.assertEqual(audit_cache_key('not a url at all'), '')
