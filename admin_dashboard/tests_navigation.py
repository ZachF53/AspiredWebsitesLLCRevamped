"""
Admin sidebar navigation.

The sidebar used to be ~200 lines of hand-written anchors. Nothing could
check it, so a duplicate Calendar Connect link sat there unnoticed and
lead-scraping tools were filed under "Clients". These tests hold the
properties that markup could not.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from admin_dashboard.navigation import NAVIGATION, all_items, is_active


User = get_user_model()


class NavigationDefinitionTests(TestCase):

    def test_every_link_resolves(self):
        """A typo in a url name renders the whole admin un-loadable,
        because the sidebar is on every page."""
        broken = []
        for item in all_items():
            try:
                reverse(item.url_name)
            except NoReverseMatch:
                broken.append(f'{item.label} -> {item.url_name}')
        self.assertEqual(broken, [])

    def test_no_two_items_point_at_the_same_page(self):
        """Calendar Connect was listed twice in the old markup."""
        targets = {}
        for item in all_items():
            targets.setdefault(reverse(item.url_name), []).append(item.label)
        duplicates = {
            url: labels for url, labels in targets.items() if len(labels) > 1
        }
        self.assertEqual(duplicates, {})

    def test_labels_are_unique(self):
        labels = [item.label for item in all_items()]
        self.assertEqual(len(labels), len(set(labels)))

    def test_every_group_has_a_reasonable_size(self):
        """A group that grows past a dozen items has become a drawer —
        which is what "Content & Settings" had turned into."""
        for group in NAVIGATION:
            with self.subTest(group=group.label):
                self.assertGreater(len(group.items), 0)
                self.assertLessEqual(len(group.items), 12)

    def test_prospecting_tools_are_not_filed_under_delivery(self):
        """Leads and scraping happen before anyone is a client."""
        by_group = {
            g.label: {i.label for i in g.items} for g in NAVIGATION
        }
        self.assertIn('Leads', by_group['Pipeline'])
        self.assertIn('Find Leads', by_group['Pipeline'])
        self.assertNotIn('Leads', by_group['Delivery'])


class ActiveStateTests(TestCase):

    def _item(self, label):
        return next(i for i in all_items() if i.label == label)

    def test_dashboard_is_active_only_on_its_own_page(self):
        dashboard = self._item('Dashboard')
        self.assertTrue(is_active(dashboard, '/admin-dashboard/'))
        self.assertFalse(is_active(dashboard, '/admin-dashboard/leads/'))

    def test_section_links_stay_active_on_child_pages(self):
        accounts = self._item('Accounts')
        self.assertTrue(is_active(accounts, '/admin-dashboard/accounts/'))
        self.assertTrue(
            is_active(accounts, '/admin-dashboard/accounts/abc-123/'))

    def test_the_deeper_of_two_nested_links_wins(self):
        """/billing/new-invoice/ must light New Invoice, not Billing.
        The old markup needed hand-written exclusions for this."""
        path = self._item('New Invoice').path
        self.assertTrue(is_active(self._item('New Invoice'), path))
        self.assertFalse(is_active(self._item('Billing'), path))

    def test_match_paths_come_from_the_url_conf(self):
        """Hand-written prefixes drifted from the routes; six of the
        first set were already wrong."""
        for item in all_items():
            with self.subTest(item=item.label):
                self.assertTrue(item.path.startswith('/'))
                self.assertTrue(is_active(item, item.path))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class NavigationRenderingTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='navstaff', email='navstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def test_sidebar_renders_every_item(self):
        html = self.client.get('/admin-dashboard/').content.decode()
        for item in all_items():
            with self.subTest(item=item.label):
                self.assertIn(f'>{item.label}</span>', html)

    def test_group_labels_render(self):
        html = self.client.get('/admin-dashboard/').content.decode()
        for group in NAVIGATION:
            if group.label:
                with self.subTest(group=group.label):
                    self.assertIn(group.label, html)

    def test_current_page_is_marked_for_assistive_tech(self):
        html = self.client.get('/admin-dashboard/').content.decode()
        self.assertIn('aria-current="page"', html)
        self.assertEqual(html.count('aria-current="page"'), 1)


class ViewModuleSplitTests(TestCase):
    """admin_dashboard/views.py is being split into per-domain modules.

    urls.py references every view as `views.<name>`, so an extraction that
    forgets to re-export a name breaks that URL at import time — and the
    admin sidebar is on every page, so one missing name takes the whole
    dashboard down rather than one route.
    """

    def test_every_url_referenced_view_resolves(self):
        import pathlib
        import re

        from admin_dashboard import views

        source = pathlib.Path(
            'admin_dashboard/urls.py').read_text(encoding='utf-8')
        names = sorted(set(re.findall(r'views\.(\w+)', source)))
        self.assertGreater(len(names), 100, 'urls.py scan found too few names')

        missing = [name for name in names if not hasattr(views, name)]
        self.assertEqual(missing, [], (
            'These views are referenced by urls.py but no longer reachable '
            'from admin_dashboard.views — re-export them from the module '
            'they moved to.'))

    def test_extracted_modules_are_importable_on_their_own(self):
        """A split module that only works when views.py imports it first
        has not really been split."""
        import importlib

        for module in ('admin_dashboard.views_pricing',
                       'admin_dashboard.views_deploy',
                       'admin_dashboard.views_onboarding_questions'):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))

    def test_no_extracted_module_has_an_undefined_name(self):
        """The first extraction pass moved views that referenced
        `_admin_context` without importing it. `manage.py check` passed —
        a NameError inside a view body only fires when the view runs — so
        the breakage surfaced as failing tests rather than a startup
        error. pyflakes catches the whole class statically.

        Widened past `admin_dashboard/views*.py` to the modules the
        cutover actually rewrote. Removing a `client = ...` lookup from
        `reporting/tasks.py` left two `client=client` kwargs behind it,
        and the uptime task — a Celery beat job with no test covering
        that branch — raised NameError every five minutes. The guard
        existed for exactly that failure and was scoped too narrowly to
        see it.
        """
        import glob
        import subprocess
        import sys

        targets = sorted(
            glob.glob('admin_dashboard/views*.py')
            + glob.glob('clients/*.py')
            + glob.glob('clients/management/commands/*.py')
            + glob.glob('reporting/*.py')
            + glob.glob('billing/*.py')
            + glob.glob('vault/*.py')
            + glob.glob('domains/*.py')
            + ['admin_dashboard/context.py'])
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pyflakes', *targets],
                capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest('pyflakes not available')
        if 'No module named' in result.stderr:
            self.skipTest('pyflakes not installed')

        undefined = [
            line for line in result.stdout.splitlines()
            if 'undefined name' in line
        ]
        self.assertEqual(undefined, [], (
            'Extracted view modules reference names they do not import. '
            'Import them from admin_dashboard.context (never from views.py '
            '— that is a circular import).'))
