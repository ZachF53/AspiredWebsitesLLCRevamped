"""
Contact-form spam layers.

Every case below is a real submission that reached the prod database, or
a real enquiry shape that must never be blocked. 35 of 35 contact-form
leads on prod turned out to be spam, and all 35 cleared the four layers
that existed at the time.
"""

from django.core.cache import cache
from django.test import TestCase

from public.views import (
    _body_fingerprint, _classify_spam, _is_repeated_body,
    _non_latin_ratio,
)


def submission(name, email, message):
    return {'name': name, 'email': email, 'message': message}


class RealSpamTests(TestCase):
    """Verbatim from the prod lead table."""

    def setUp(self):
        cache.clear()

    def test_multilingual_price_bot_is_blocked(self):
        """One bot, eight languages, eight IPs, same intent. The per-IP
        caps never tripped because no single address repeated."""
        for message in (
            'Здравейте, исках да знам цената ви.',
            'হাই, আমি আপনার মূল্য জানতে চেয়েছিলাম.',
            'Ndewo, achọrọ m ịmara ọnụahịa gị.',
        ):
            with self.subTest(message=message[:20]):
                self.assertTrue(_classify_spam(
                    submission('RobertBiz', 'a@b.com', message)))

    def test_advance_fee_scam_is_blocked(self):
        self.assertTrue(_classify_spam(submission(
            'Andrew Walters', 'a@gmail.com',
            'Dear Beloved, My name is Mr. Andrew Walters. I have a '
            'business proposal for you regarding my late client.')))

    def test_seo_solicitations_are_blocked(self):
        for message in (
            'Hello, Are you seeking professional partner SEO company?',
            'Hello Team, We specialize in offering high-quality backlinks.',
            'Hi, I am putting together a small backlink collaboration.',
        ):
            with self.subTest(message=message[:24]):
                self.assertTrue(_classify_spam(
                    submission('Jason Roberts', 'a@b.com', message)))

    def test_bot_name_suffix_is_blocked(self):
        """RobertBiz submitted eight times. Real people do not append a
        business suffix to their first name."""
        self.assertTrue(_classify_spam(submission(
            'RobertBiz', 'a@b.com',
            'Hello, I wanted to ask about pricing for a new website.')))


class GenuineEnquiryTests(TestCase):
    """SYNTHETIC. Invented for this test file, not drawn from the
    database - prod has never received a genuine contact-form enquiry.
    All 35 real submissions were spam.

    They exist because false positives are the expensive failure here. A
    blocked spam costs nothing; a blocked prospect costs a client and
    nobody ever finds out it happened.
    """

    def setUp(self):
        cache.clear()

    def test_ordinary_enquiry_passes(self):
        self.assertEqual(_classify_spam(submission(
            'Sarah Chen', 'sarah@chenlaw.com',
            'Hi, we are a small family law firm in Houston and our '
            'website is 8 years old. Looking for a quote on a rebuild.')),
            '')

    def test_spanish_enquiry_passes(self):
        """Spanish-speaking Texas businesses are a wanted audience, so
        the script rule covers non-Latin alphabets only."""
        self.assertEqual(_classify_spam(submission(
            'Miguel Ramirez', 'miguel@ramirezdental.com',
            'Hola, tengo una clinica dental en San Antonio y necesito '
            'un sitio web nuevo. Cuanto cuesta?')),
            '')

    def test_short_first_name_passes(self):
        self.assertEqual(_classify_spam(submission(
            'Bob', 'bob@smithlaw.com',
            'Need a new site for my practice. Can you call me this week '
            'to discuss what is involved?')),
            '')

    def test_mentioning_seo_in_a_genuine_enquiry_passes(self):
        """A prospect asking about SEO is a customer, not a spammer."""
        self.assertEqual(_classify_spam(submission(
            'Dana Wills', 'dana@willslaw.com',
            'Our current site has terrible SEO and we want to rank for '
            'personal injury in Dallas. Can you help with that?')),
            '')


class NonLatinRatioTests(TestCase):

    def test_pure_english_is_zero(self):
        self.assertEqual(_non_latin_ratio('Hello, I need a website.'), 0.0)

    def test_accented_latin_is_not_foreign(self):
        """Spanish, Italian and French are Latin script."""
        self.assertEqual(_non_latin_ratio('Cuanto cuesta un sitio web?'), 0.0)
        self.assertEqual(_non_latin_ratio('Sveiki, es gribeju zinat.'), 0.0)

    def test_cyrillic_is_fully_foreign(self):
        self.assertGreater(_non_latin_ratio('Здравейте, исках'), 0.9)

    def test_punctuation_and_digits_do_not_dilute(self):
        self.assertGreater(_non_latin_ratio('Здравейте, 2026!!!'), 0.9)

    def test_empty_is_zero(self):
        self.assertEqual(_non_latin_ratio(''), 0.0)
        self.assertEqual(_non_latin_ratio(None), 0.0)


class RepeatedBodyTests(TestCase):
    """Three identical SEO pitches arrived from three submissions. The
    per-IP caps cannot see that; a fingerprint across all senders can."""

    def setUp(self):
        cache.clear()

    def test_third_identical_message_is_blocked(self):
        body = 'Hello, are you interested in a partnership opportunity?'
        self.assertFalse(_is_repeated_body(body)[0])
        self.assertFalse(_is_repeated_body(body)[0])
        self.assertTrue(_is_repeated_body(body)[0])

    def test_different_messages_do_not_collide(self):
        for i in range(5):
            self.assertFalse(
                _is_repeated_body(f'Unique enquiry number {i}')[0])

    def test_fingerprint_ignores_case_and_punctuation(self):
        """A bot re-sending with a new greeting still collides."""
        self.assertEqual(
            _body_fingerprint('Hello, we can rank your site!'),
            _body_fingerprint('hello we can rank your site'))

    def test_blank_message_is_never_a_repeat(self):
        self.assertFalse(_is_repeated_body('')[0])
        self.assertFalse(_is_repeated_body(None)[0])
