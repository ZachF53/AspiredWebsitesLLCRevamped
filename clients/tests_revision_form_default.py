"""RevisionForm's "Is this a major change?" checkbox starts unchecked
for the client-facing portal form (model default stays True — the
admin/AI-assistant path via clients.services.add_revision is separate
and untouched)."""

from django.test import TestCase

from clients.forms import RevisionForm


class RevisionFormDefaultTests(TestCase):

    def test_is_major_starts_unchecked(self):
        form = RevisionForm()
        self.assertFalse(form.fields['is_major'].initial)

    def test_model_default_is_unchanged(self):
        from clients.models import RevisionRequest
        self.assertTrue(
            RevisionRequest._meta.get_field('is_major').default)
