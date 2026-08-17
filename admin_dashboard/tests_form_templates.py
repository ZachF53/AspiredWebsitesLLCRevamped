"""
A template must not render a form field the form does not have.

Django resolves `{{ form.client }}` on a form with no `client` field to
the empty string, silently. So renaming a field to its canonical
equivalent leaves the template rendering *nothing* where a required
picker used to be — the page still returns 200, still looks broadly
right, and the form fails validation on submit with an error pointing at
a field the user was never shown.

This caught exactly that during the Website cutover: `SiteChangelogForm`
moved from `client` to `website_new` and the add-entry template kept
asking for `form.client`.
"""

import pathlib
import re

from django.test import SimpleTestCase

# `{{ form.<name> ... }}` — the attribute directly after `form.`
FORM_FIELD = re.compile(r'\{\{\s*form\.([a-z_][a-z0-9_]*)')

# Attributes every bound form exposes, which are not field names.
FORM_ATTRIBUTES = {
    'errors', 'non_field_errors', 'as_p', 'as_table', 'as_ul', 'as_div',
    'media', 'instance', 'is_valid', 'cleaned_data', 'initial', 'fields',
    'hidden_fields', 'visible_fields', 'management_form', 'empty_form',
    'forms', 'prefix', 'auto_id', 'is_bound', 'changed_data',
}

# template file -> the form class it renders
TEMPLATE_FORMS = {
    'admin_dashboard/changelog_add.html':
        ('admin_dashboard.forms', 'SiteChangelogForm'),
    'admin_dashboard/blog_generate.html':
        ('admin_dashboard.forms', 'BlogGenerateForm'),
}


class FormFieldTemplateTests(SimpleTestCase):

    def _template_path(self, name):
        return (pathlib.Path('admin_dashboard') / 'templates' / name)

    def test_every_rendered_field_exists_on_its_form(self):
        import importlib

        for template_name, (module_path, form_name) in TEMPLATE_FORMS.items():
            path = self._template_path(template_name)
            with self.subTest(template=template_name):
                self.assertTrue(path.exists(), f'{path} is missing')
                source = path.read_text(encoding='utf-8')

                form_class = getattr(
                    importlib.import_module(module_path), form_name)
                available = set(form_class().fields) | FORM_ATTRIBUTES

                rendered = set(FORM_FIELD.findall(source))
                unknown = rendered - available
                self.assertEqual(
                    unknown, set(),
                    f'{template_name} renders {sorted(unknown)}, which '
                    f'{form_name} does not define. Django renders these as '
                    'empty strings, so the field simply vanishes from the '
                    'page instead of erroring.')

    def test_the_changelog_form_is_website_scoped(self):
        """The rename that motivated this test."""
        from admin_dashboard.forms import SiteChangelogForm

        fields = set(SiteChangelogForm().fields)
        self.assertIn('website_new', fields)
        self.assertNotIn('client', fields)

    def test_the_blog_form_is_website_scoped(self):
        from admin_dashboard.forms import BlogGenerateForm

        fields = set(BlogGenerateForm().fields)
        self.assertIn('website', fields)
        self.assertNotIn('client', fields)
