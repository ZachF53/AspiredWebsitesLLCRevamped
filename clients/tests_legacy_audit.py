"""
The legacy-dependency classifier must not over-report.

The predecessor grepped for the substring and reported 70 blocking modules
when only 49 contained a live read. That mattered: the gate is the thing
standing between a healthy database and an irreversible DROP TABLE, and a
gate whose number is 40% noise is a gate nobody reads carefully. These tests
pin the three-way split so it cannot silently regress to a grep.
"""

from django.test import SimpleTestCase

from clients.legacy_audit import analyse_source, scan_repository, summarise


class AnalyseSourceTests(SimpleTestCase):

    def test_a_docstring_mention_is_prose_not_a_blocker(self):
        source = (
            '"""scope is a Website (per-site) or a ClientProfile (legacy)."""\n'
            'VALUE = 1\n'
        )
        report = analyse_source(source)
        self.assertEqual(report.code_lines, [])
        self.assertFalse(report.blocks_removal)
        self.assertEqual(report.prose_count, 1)

    def test_a_comment_mention_is_prose_not_a_blocker(self):
        source = '# ClientProfile used to own this row.\nVALUE = 1\n'
        report = analyse_source(source)
        self.assertEqual(report.code_lines, [])
        self.assertFalse(report.blocks_removal)
        self.assertEqual(report.prose_count, 1)

    def test_a_function_docstring_is_prose(self):
        source = (
            'def scope_filter(scope):\n'
            '    """`scope` is a Website or a ClientProfile (legacy)."""\n'
            '    return {}\n'
        )
        report = analyse_source(source)
        self.assertFalse(report.blocks_removal)
        self.assertEqual(report.prose_count, 1)

    def test_an_import_is_a_live_read(self):
        source = 'from clients.models import ClientProfile\n'
        report = analyse_source(source)
        self.assertEqual(report.code_lines, [1])
        self.assertTrue(report.blocks_removal)

    def test_a_queryset_is_a_live_read(self):
        source = (
            'def find(pk):\n'
            '    return ClientProfile.objects.filter(pk=pk)\n'
        )
        report = analyse_source(source)
        self.assertEqual(report.code_lines, [2])
        self.assertTrue(report.blocks_removal)

    def test_an_isinstance_check_is_a_live_read(self):
        source = (
            'def is_legacy(obj):\n'
            '    return isinstance(obj, ClientProfile)\n'
        )
        report = analyse_source(source)
        self.assertTrue(report.blocks_removal)

    def test_a_foreign_key_declaration_is_schema_not_a_blocker(self):
        """The drop migration removes these. Counting them as blockers
        would require the migration to have run before allowing it to run."""
        source = (
            'class Snapshot(models.Model):\n'
            '    client = models.ForeignKey(\n'
            "        ClientProfile,\n"
            "        on_delete=models.CASCADE,\n"
            "        related_name='snapshots',\n"
            '    )\n'
        )
        report = analyse_source(source)
        self.assertEqual(report.code_lines, [])
        self.assertFalse(report.blocks_removal)
        # The whole multi-line call counts, not just its opening line.
        self.assertEqual(len(report.schema_lines), 5)

    def test_a_lazy_string_foreign_key_is_schema(self):
        source = (
            'class Row(models.Model):\n'
            "    project = models.ForeignKey('clients.Project',\n"
            '                                on_delete=models.CASCADE)\n'
        )
        report = analyse_source(source)
        self.assertFalse(report.blocks_removal)
        self.assertTrue(report.schema_lines)

    def test_a_live_read_beside_a_declaration_still_blocks(self):
        """A model module that both declares an FK and queries the legacy
        model is not off the hook because of the declaration."""
        source = (
            'from clients.models import ClientProfile\n'
            'class Row(models.Model):\n'
            '    client = models.ForeignKey(ClientProfile,\n'
            '                               on_delete=models.CASCADE)\n'
            'def orphans():\n'
            '    return ClientProfile.objects.filter(user=None)\n'
        )
        report = analyse_source(source)
        self.assertTrue(report.blocks_removal)
        self.assertIn(6, report.code_lines)
        self.assertNotIn(3, report.code_lines)

    def test_unrelated_source_reports_nothing(self):
        report = analyse_source('from clients.models import Account\n')
        self.assertFalse(report.blocks_removal)
        self.assertEqual(report.schema_lines, [])
        self.assertEqual(report.prose_count, 0)

    def test_a_syntax_error_is_not_reported_as_a_dependency(self):
        self.assertIsNone(analyse_source('def broken(\n'))


class ScanRepositoryTests(SimpleTestCase):
    """The scan runs over the real tree, so these assert invariants rather
    than fixed counts -- a count would fail on every conversion commit."""

    def setUp(self):
        self.reports = scan_repository()

    def test_the_transition_machinery_is_not_reported_against_itself(self):
        paths = {r.path for r in self.reports}
        for allowed in ('clients/models.py', 'clients/parity.py',
                        'clients/canonical_stamping.py'):
            self.assertNotIn(allowed, paths)

    def test_no_migration_is_reported(self):
        for report in self.reports:
            self.assertNotIn('migrations/', report.path)
            self.assertNotIn('migrations_planned/', report.path)

    def test_no_test_module_is_reported(self):
        for report in self.reports:
            self.assertNotIn('test', report.path.rsplit('/', 1)[-1])

    def test_blocking_modules_are_a_subset_of_all_reported(self):
        totals = summarise(self.reports)
        self.assertLessEqual(totals['blocking_modules'], totals['modules'])

    def test_the_blocking_count_excludes_prose_only_modules(self):
        """The regression that motivated this module: prose-only files were
        counted as blockers."""
        totals = summarise(self.reports)
        prose_only = [
            r for r in self.reports
            if not r.code_lines and not r.schema_lines and r.prose_count]
        self.assertEqual(
            totals['blocking_modules'],
            len([r for r in self.reports
                 if r.code_lines and not r.reports_on_cutover]))
        for report in prose_only:
            self.assertFalse(report.blocks_removal)

    def test_reports_are_ordered_by_remaining_work(self):
        counts = [len(r.code_lines) for r in self.reports]
        self.assertEqual(counts, sorted(counts, reverse=True))


class LegacyAttributeDetectionTests(SimpleTestCase):
    """Counting symbols is not the same as counting dependence.

    `request.client_profile` is set by the portal decorator and read by
    ~20 views. None of them names ClientProfile, so a scan that looks
    only for the class reported every one of those modules as clean while
    they would break outright at drop time — the gate said
    clients/views.py had zero legacy reads when it had twenty.
    """

    def test_a_request_attribute_read_is_a_live_read(self):
        source = (
            'def portal_view(request):\n'
            '    profile = request.client_profile\n'
            '    return profile\n'
        )
        report = analyse_source(source)
        self.assertEqual(report.code_lines, [2])
        self.assertTrue(report.blocks_removal)

    def test_an_assignment_to_it_also_counts(self):
        """The decorator that sets it is a dependency too."""
        source = (
            'def wrapper(request, profile):\n'
            '    request.client_profile = profile\n'
        )
        self.assertTrue(analyse_source(source).blocks_removal)

    def test_a_module_naming_only_the_attribute_is_still_scanned(self):
        """The pre-filter skipped any file that never said
        'ClientProfile', so a module using only the request attribute was
        never even parsed. domains/views.py was invisible for that
        reason."""
        from clients.legacy_audit import LEGACY_ATTRIBUTES, LEGACY_NAMES

        source = 'x = request.client_profile\n'
        self.assertFalse(any(n in source for n in LEGACY_NAMES))
        self.assertTrue(any(a in source for a in LEGACY_ATTRIBUTES))
        self.assertTrue(analyse_source(source).blocks_removal)

    def test_an_unrelated_attribute_is_not_flagged(self):
        report = analyse_source('x = request.account\n')
        self.assertFalse(report.blocks_removal)


class CutoverReportingExclusionTests(SimpleTestCase):
    """A module that REPORTS on the cutover cannot block it.

    data_health_views renders the progress panel — legacy row counts,
    orphan counts, how much is left. It reads the legacy models on
    purpose and is deleted alongside them, so blocking on it is circular
    in exactly the way blocking on FK declarations would be.

    Narrow on purpose: this is an allowlist of one path, not a heuristic.
    Anything else that reads the legacy models still blocks.
    """

    def test_the_data_health_panel_does_not_block(self):
        reports = {r.path: r for r in scan_repository()}
        panel = reports.get('admin_dashboard/data_health_views.py')
        self.assertIsNotNone(
            panel, 'the panel should still be REPORTED, just not blocking')
        self.assertTrue(panel.code_lines, 'it does read the legacy models')
        self.assertTrue(panel.reports_on_cutover)
        self.assertFalse(panel.blocks_removal)

    def test_the_exclusion_is_an_allowlist_not_a_pattern(self):
        from clients.legacy_audit import REPORTING_ON_THE_CUTOVER

        self.assertEqual(
            REPORTING_ON_THE_CUTOVER,
            ('admin_dashboard/data_health_views.py',))

    def test_an_ordinary_module_with_reads_still_blocks(self):
        from clients.legacy_audit import ModuleReport

        ordinary = ModuleReport(
            path='some/view.py', code_lines=[1], schema_lines=[],
            prose_count=0)
        self.assertTrue(ordinary.blocks_removal)
