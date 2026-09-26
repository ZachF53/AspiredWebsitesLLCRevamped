"""Expanded case studies: content module sanity and page rendering."""
from django.test import TestCase
from django.utils import timezone

from clients.case_study_deep_dive import CONTENT, NARRATIVE
from clients.models import CaseStudy


class ContentModuleTests(TestCase):
    def test_every_section_has_heading_and_points(self):
        for slug, content in CONTENT.items():
            for part in content['deep_dive']:
                self.assertTrue(part.get('heading'), slug)
                self.assertTrue(part.get('points'), f'{slug}: {part}')
                for point in part['points']:
                    self.assertTrue(point.strip(), slug)

    def test_scorecards_are_dated_and_pairs_are_value_label(self):
        for slug, content in CONTENT.items():
            sc = content['scorecard']
            if not sc:
                continue
            self.assertEqual(sc['measured_on'], '2026-09-26', slug)
            for pair in sc.get('vitals', []):
                self.assertEqual(len(pair), 2, slug)

    def test_no_unverified_security_claims_for_burgland(self):
        # The live site sends neither header (checked 2026-09-26).
        text = str(CONTENT['burgland-technologies']).lower()
        text += NARRATIVE['burgland-technologies']['solution'].lower()
        self.assertNotIn('strict transport', text)
        self.assertNotIn('hsts', text)
        self.assertNotIn('content security policy', text)

    def test_maintained_site_gets_no_scorecard(self):
        self.assertEqual(CONTENT['denis-law-group']['scorecard'], {})


class DetailPageTests(TestCase):
    def _study(self, **extra):
        return CaseStudy.objects.create(
            slug='deep-dive-test', title='Deep Dive Test',
            is_published=True, published_at=timezone.now(),
            engagement_type='built', **extra)

    def test_scorecard_and_deep_dive_render(self):
        content = CONTENT['whitehead-wellness']
        self._study(scorecard=content['scorecard'],
                    deep_dive=content['deep_dive'])
        resp = self.client.get('/portfolio/deep-dive-test/')
        self.assertContains(resp, 'Measured, Not Claimed')
        self.assertContains(resp, 'September 26, 2026')
        self.assertContains(resp, '11,682 checks since Aug 17, 2026')
        self.assertContains(resp, '0 critical')
        self.assertContains(resp, 'How It Was Built')
        self.assertContains(resp, 'The Membership Engine')
        # Value renders before its label.
        body = resp.content.decode()
        self.assertLess(body.index('0 ms'),
                        body.index('Total blocking time on a phone'))

    def test_lighthouse_circles_only_when_scored(self):
        self._study(scorecard={
            'measured_on': '2026-09-26',
            'lighthouse': {'performance': 97, 'accessibility': 100,
                           'best_practices': 100, 'seo': 100}})
        resp = self.client.get('/portfolio/deep-dive-test/')
        self.assertContains(resp, 'case-score--good')
        self.assertContains(resp, 'Best Practices')

    def test_empty_study_omits_new_sections(self):
        self._study()
        resp = self.client.get('/portfolio/deep-dive-test/')
        self.assertNotContains(resp, 'Measured, Not Claimed')
        self.assertNotContains(resp, 'case-deep-dive')
