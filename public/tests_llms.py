from django.core.management import call_command
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class LlmsTxtTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing', verbosity=0)

    def test_served_as_plain_text_with_the_required_header(self):
        resp = self.client.get('/llms.txt')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/plain'))
        body = resp.content.decode()
        self.assertTrue(body.startswith('# Aspired Websites\n'))
        self.assertIn('\n> ', body)

    def test_prices_come_from_the_database(self):
        from billing.pricing_models import ServiceTier
        tier = ServiceTier.objects.get(slug='hvac-build-full')
        tier.price = 2345
        tier.save()
        self.assertIn('$2,345 paid in full', self.client.get('/llms.txt').content.decode())

    def test_links_always_point_at_production(self):
        body = self.client.get('/llms.txt').content.decode()
        self.assertIn('https://aspiredwebsites.com/pricing/', body)
        self.assertNotIn('staging.', body)
