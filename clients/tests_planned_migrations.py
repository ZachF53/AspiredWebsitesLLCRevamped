"""
The planned drop migrations must stay promotable.

`clients/migrations_planned/` holds the legacy-removal migrations outside
the migration graph on purpose -- the deploy script runs `migrate`
unconditionally, and dropping ClientProfile/Project is a separately
approved operation, not something that rides along with a template fix.

The cost of keeping them outside the graph is that nothing checks them.
They rot silently, and the rot only surfaces at promotion time -- the
single moment when you least want a surprise, because by then a backup
has been taken and a maintenance window is open.

Two ways they rot:

`dependencies`
    Each names a specific leaf migration. Every ordinary migration added
    since pushes that leaf forward, and a promoted file depending on an
    old one leaves the app with two leaf nodes: "Conflicting migrations
    detected; multiple leaf nodes in the migration graph." All nine
    files were stale by four migrations when this test was written.

`RemoveIndex` coverage
    A composite index covering a column must be dropped before the
    column. The rehearsal died on exactly this
    (`FieldDoesNotExist: NewGbpPerformanceSnapshot has no field named
    'client'`), because SQLite rebuilds the table and the new one has no
    such column for the index to cover.

Neither needs a database, so this is cheap enough to run on every commit.
"""

import pathlib
import re

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase


PLANNED_DIR = pathlib.Path(__file__).resolve().parent / 'migrations_planned'

DEPENDENCY_RE = re.compile(r"\('([a-z_]+)', '([^']+)'\)")
REMOVE_INDEX_RE = re.compile(
    r"RemoveIndex\(model_name='([a-z_]+)', name='([^']+)'\)")
REMOVE_FIELD_RE = re.compile(
    r"RemoveField\(model_name='([a-z_]+)', name='([a-z_]+)'\)")


def _planned_files():
    return sorted(PLANNED_DIR.glob('*.planned'))


def _leaf_migrations():
    """{app_label: leaf migration name} for every app in the graph."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return {app: name for app, name in loader.graph.leaf_nodes()}


class PlannedMigrationDependencyTests(SimpleTestCase):

    def test_there_are_planned_files_to_check(self):
        """Guards the three tests below from passing on an empty glob."""
        self.assertGreaterEqual(len(_planned_files()), 9)

    def test_every_dependency_names_the_current_leaf(self):
        leaves = _leaf_migrations()
        stale = []
        for path in _planned_files():
            source = path.read_text(encoding='utf-8')
            body = source.split('dependencies', 1)[-1].split(']', 1)[0]
            for app, name in DEPENDENCY_RE.findall(body):
                # Phase 2 depends on the phase 1 migrations, which have no
                # numbers until they are promoted. Those are placeholders,
                # not stale references.
                if name.startswith('<'):
                    continue
                leaf = leaves.get(app)
                if leaf is not None and name != leaf:
                    stale.append(f'{path.name}: {app} -> {name} (leaf: {leaf})')

        self.assertEqual(
            stale, [],
            'Planned migrations depend on migrations that are no longer '
            f'the leaf: {stale}. Promoting one would branch the graph. '
            'Re-point the dependency at the current leaf.')

    def test_the_planned_directory_cannot_be_imported_as_migrations(self):
        """The whole safety property. If this ever becomes a package,
        `manage.py migrate` finds it and the next routine deploy drops
        the legacy tables."""
        self.assertFalse((PLANNED_DIR / '__init__.py').exists())
        for path in _planned_files():
            self.assertTrue(path.name.endswith('.py.planned'), path.name)


class PlannedMigrationIndexTests(SimpleTestCase):
    """Every removed column must have its covering indexes removed first."""

    LEGACY = frozenset({'client', 'project'})

    def _covering_indexes(self):
        """{(app_label, model_name): [index_name, ...]} for declared
        indexes whose fields include a legacy owner column."""
        from django.apps import apps

        covering = {}
        for model in apps.get_models():
            for index in model._meta.indexes:
                fields = {f.lstrip('-') for f in index.fields}
                if self.LEGACY & fields:
                    key = (model._meta.app_label, model._meta.model_name)
                    covering.setdefault(key, []).append(index.name)
        return covering

    def test_every_covering_index_is_removed_before_its_column(self):
        removed_indexes = set()
        removed_fields = set()
        for path in _planned_files():
            source = path.read_text(encoding='utf-8')
            app = path.name.split('_drop_')[0].replace('phase1_', '')
            for model_name, index_name in REMOVE_INDEX_RE.findall(source):
                removed_indexes.add(index_name)
                # Ordering matters as much as presence: the RemoveIndex
                # must appear above the RemoveField for the same model.
                index_at = source.index(f"name='{index_name}'")
                field_at = source.find(
                    f"RemoveField(model_name='{model_name}'")
                if field_at != -1:
                    self.assertLess(
                        index_at, field_at,
                        f'{path.name}: RemoveIndex {index_name} must come '
                        f'before RemoveField on {model_name}.')
            for model_name, field in REMOVE_FIELD_RE.findall(source):
                removed_fields.add((app, model_name, field))

        missing = []
        for (app, model_name), names in self._covering_indexes().items():
            drops_the_column = any(
                (a, m, f) in removed_fields
                for a, m, f in [(app, model_name, 'client'),
                                (app, model_name, 'project')])
            if not drops_the_column:
                continue
            for name in names:
                if name not in removed_indexes:
                    missing.append(f'{app}.{model_name}: {name}')

        self.assertEqual(
            missing, [],
            'These indexes cover a column the planned migrations drop, but '
            f'no RemoveIndex removes them first: {missing}. The rehearsal '
            'fails with FieldDoesNotExist during the table rebuild.')
