from django.test import TestCase, override_settings
from django.urls import reverse


class SiteVerificationMetaTests(TestCase):
    """
    Search-console ownership tags (Master Plan Phase 0, §10).

    These render from settings via core.context_processors.
    site_verification. The important guarantee is that they are
    *inert* when unset — a blank token must emit no tag at all, not an
    empty one, because an empty content="" is a malformed verification
    tag that Google reports as a failed check.
    """

    def test_no_meta_tags_when_tokens_unset(self):
        """Default (blank) config renders neither verification tag."""
        with override_settings(GOOGLE_SITE_VERIFICATION='',
                               BING_SITE_VERIFICATION=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('google-site-verification', html)
        self.assertNotIn('msvalidate.01', html)

    def test_google_tag_renders_when_token_set(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='test-google-token',
                               BING_SITE_VERIFICATION=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn(
            '<meta name="google-site-verification" '
            'content="test-google-token">', html)
        self.assertNotIn('msvalidate.01', html)

    def test_bing_tag_renders_when_token_set(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='',
                               BING_SITE_VERIFICATION='test-bing-token'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn(
            '<meta name="msvalidate.01" content="test-bing-token">', html)
        self.assertNotIn('google-site-verification', html)

    def test_both_tags_render_together(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='g-token',
                               BING_SITE_VERIFICATION='b-token'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn('content="g-token"', html)
        self.assertIn('content="b-token"', html)

    def test_token_is_html_escaped(self):
        """A token is untrusted config — it must not break out of the tag."""
        with override_settings(
                GOOGLE_SITE_VERIFICATION='a"><script>x</script>',
                BING_SITE_VERIFICATION=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('<script>x</script>', html)
        self.assertIn('&quot;&gt;&lt;script&gt;', html)
