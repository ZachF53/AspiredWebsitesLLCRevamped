"""
Regression tests for outbound copy validation.

The bodies in REAL_REJECTIONS are verbatim copies of messages that were
actually delivered to real prospects between 2026-06-16 and 2026-08-01,
because nothing in the pipeline checked that the model's output was an
email. If any of these ever passes validation again, this suite fails.
"""

from django.test import TestCase

from outreach.copy_guard import describe_copy_problems, is_sendable

# Verbatim from prod EmailSent rows.
REAL_REJECTIONS = [
    (
        'Quick question, Vilma Sikes',
        "I don't have enough specific details about Vilma Sikes's business "
        "to make a genuine, accurate observation — I only know the name and "
        "industry (retail). Making something up would violate your "
        "instructions.\n\n"
        "To write this email properly, could you provide at least one of "
        "the following?\n\n"
        "- **Website URL** (so I can note a real observation like "
        "PageSpeed, missing HTTPS, no SSL, etc.)\n"
        "- **City/location** (for a location-based reference)\n"
        "- **Any other detail** about the business (social presence, store "
        "type, etc.)\n\n"
        "Once you share one real data point, I'll write the email."
    ),
    (
        'Quick question, RobertBiz',
        "I don't have enough specific details about RobertBiz (no website "
        "URL, location, PageSpeed data, or other observable facts) to make "
        "a genuine specific observation — and my instructions are clear "
        "that I should never make up a fact about them.\n\n"
        "Could you provide any of the following so I can write an "
        "accurate, personalized email?\n\n"
        "- Their website URL\n- Their city/region\n"
        "- A PageSpeed score or security issue you've observed\n"
        "- Any other real detail about their business\n\n"
        "That way the email will feel genuine rather than generic."
    ),
    (
        'Quick question, RobertBiz',
        "I don't have enough specific details about RobertBiz to make a "
        "genuine, accurate observation about their website or business — "
        "and I won't fabricate one.\n\n"
        "To write this email properly, could you provide at least one of "
        "the following:\n\n"
        "- **Their website URL** (so I can note a real observation like "
        "missing HTTPS, slow load times, etc.)\n"
        "- **Their city/region**\n"
        "- **A specific detail about their retail business**\n\n"
        "Even one real detail will let me write something that feels "
        "personal rather than generic."
    ),
]

GOOD_EMAIL = (
    'Quick note about nyccriminalattorneys.com',
    "Hi Robert,\n\n"
    "I came across nyccriminalattorneys.com and noticed it loads at 41/100 "
    "on mobile, which usually means visitors on phones bounce before the "
    "page finishes. For a criminal defense practice that's often the "
    "majority of your traffic.\n\n"
    "I build fast, secure sites for law firms — I've got a Masters in "
    "Cybersecurity and a CISSP, so security isn't an afterthought.\n\n"
    "Worth a quick look at your numbers?\n\n"
    "— Zachery"
)

THIN_BUT_VALID = (
    'Quick question about your website',
    "Hi Vilma,\n\n"
    "I build custom websites for small retail businesses across Texas and "
    "Georgia. I'm reaching out because most retail sites I see were built "
    "on a template years ago and quietly lose mobile customers.\n\n"
    "I have a Masters in Cybersecurity and a CISSP, so I handle the "
    "security side properly rather than bolting it on later.\n\n"
    "Would it be useful if I took a quick look at your site?\n\n"
    "— Zachery"
)


class CopyGuardTests(TestCase):
    def test_every_real_rejection_is_blocked(self):
        for i, (subject, body) in enumerate(REAL_REJECTIONS):
            problems = describe_copy_problems(subject, body)
            self.assertTrue(
                problems,
                f'REAL_REJECTIONS[{i}] passed validation — it was actually '
                f'delivered to a prospect and must never pass again.')
            self.assertFalse(is_sendable(subject, body))

    def test_good_email_passes(self):
        self.assertEqual(describe_copy_problems(*GOOD_EMAIL), [])
        self.assertTrue(is_sendable(*GOOD_EMAIL))

    def test_thin_but_valid_email_passes(self):
        # The correct output for a data-poor lead: generic, no invented
        # facts, no questions back to the operator. Must NOT be blocked.
        self.assertEqual(describe_copy_problems(*THIN_BUT_VALID), [])

    def test_empty_subject_or_body(self):
        self.assertIn('subject is empty', describe_copy_problems('', 'x' * 200))
        self.assertIn('body is empty', describe_copy_problems('Hi', '   '))

    def test_markdown_is_blocked(self):
        s, b = GOOD_EMAIL
        self.assertTrue(describe_copy_problems(s, b + '\n\n- **bold bullet**'))
        self.assertTrue(describe_copy_problems(s, b + '\n\n## Heading'))

    def test_placeholders_are_blocked(self):
        s, b = GOOD_EMAIL
        self.assertTrue(describe_copy_problems(s, b + '\n\nRegards, [Your Name]'))
        self.assertTrue(describe_copy_problems(s, b + '\n\nHi {{first_name}}'))

    def test_length_bounds(self):
        self.assertTrue(describe_copy_problems('Hi', 'Too short.'))
        self.assertTrue(describe_copy_problems('Hi', 'word ' * 500))

    def test_ai_self_reference_blocked(self):
        self.assertTrue(describe_copy_problems(
            'Hi', 'As an AI, I cannot browse their website. ' + 'word ' * 30))


class SplitSubjectBodyTests(TestCase):
    """The exact defect: no Subject: line meant a fabricated subject."""

    def _split(self, text):
        from outreach.sender import _split_subject_body

        class _Lead:
            firm_name = 'Vilma Sikes'
        return _split_subject_body(text, _Lead(), 1)

    def test_missing_subject_line_now_raises(self):
        from outreach.sender import EmailCopyRejected

        raw = REAL_REJECTIONS[0][1]
        with self.assertRaises(EmailCopyRejected) as ctx:
            self._split(raw)
        self.assertIn('Subject:', str(ctx.exception))

    def test_well_formed_output_parses(self):
        raw = (f'Subject: {THIN_BUT_VALID[0]}\n\n{THIN_BUT_VALID[1]}')
        subject, body = self._split(raw)
        self.assertEqual(subject, THIN_BUT_VALID[0])
        self.assertIn('custom websites', body)

    def test_subject_present_but_body_is_a_refusal_still_raises(self):
        from outreach.sender import EmailCopyRejected

        raw = f'Subject: Quick question\n\n{REAL_REJECTIONS[1][1]}'
        with self.assertRaises(EmailCopyRejected):
            self._split(raw)

    def test_empty_subject_value_raises(self):
        from outreach.sender import EmailCopyRejected

        with self.assertRaises(EmailCopyRejected):
            self._split('Subject:\n\n' + THIN_BUT_VALID[1])
