import json

from django.core.cache import cache
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CspReportingTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_public_policy_declares_report_endpoints(self):
        resp = self.client.get('/privacy-policy/')
        self.assertIn('report-uri /csp-report/; report-to csp',
                      resp['Content-Security-Policy'])
        self.assertIn('csp="http://testserver/csp-report/"',
                      resp['Reporting-Endpoints'])

    def test_legacy_report_format_is_logged(self):
        body = {'csp-report': {'document-uri': 'https://aspiredwebsites.com/',
                               'violated-directive': 'script-src',
                               'blocked-uri': 'https://evil.example/x.js'}}
        with self.assertLogs('security.csp', level='WARNING') as logs:
            resp = self.client.post('/csp-report/', json.dumps(body),
                                    content_type='application/csp-report')
        self.assertEqual(resp.status_code, 204)
        self.assertIn('evil.example', logs.output[0])

    def test_reporting_api_format_is_logged(self):
        body = [{'type': 'csp-violation',
                 'body': {'documentURL': 'https://aspiredwebsites.com/',
                          'effectiveDirective': 'img-src',
                          'blockedURL': 'https://tracker.example/p.gif'}}]
        with self.assertLogs('security.csp', level='WARNING') as logs:
            resp = self.client.post('/csp-report/', json.dumps(body),
                                    content_type='application/reports+json')
        self.assertEqual(resp.status_code, 204)
        self.assertIn('tracker.example', logs.output[0])

    def test_extension_noise_is_dropped(self):
        body = {'csp-report': {'blocked-uri': 'chrome-extension://abc/inject.js'}}
        with self.assertNoLogs('security.csp', level='WARNING'):
            resp = self.client.post('/csp-report/', json.dumps(body),
                                    content_type='application/csp-report')
        self.assertEqual(resp.status_code, 204)

    def test_rejects_get_oversize_and_garbage(self):
        self.assertEqual(self.client.get('/csp-report/').status_code, 405)
        big = json.dumps({'csp-report': {'x': 'a' * 20000}})
        self.assertEqual(self.client.post(
            '/csp-report/', big, content_type='application/csp-report').status_code, 413)
        self.assertEqual(self.client.post(
            '/csp-report/', 'not json', content_type='application/csp-report').status_code, 400)


class ScreenshotSrcsetTests(TestCase):

    def test_no_srcset_without_the_small_copy(self):
        from clients.models import CaseStudy
        study = CaseStudy(title='x', screenshot='portfolio/does-not-exist.webp')
        self.assertEqual(study.screenshot_srcset, '')

    def test_variant_name(self):
        from clients.screenshot_variants import variant_name
        self.assertEqual(variant_name('portfolio/a.webp'), 'portfolio/a-600w.webp')
