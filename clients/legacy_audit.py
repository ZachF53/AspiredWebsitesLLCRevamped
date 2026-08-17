"""
Find what still depends on the legacy ClientProfile/Project models.

The first version of this lived inside the readiness command and worked by
grepping each file for the substring `ClientProfile`. That over-counted
badly. A docstring reading "scope is a Website (per-site) or a ClientProfile
(legacy)" is not a dependency -- prose does not break when a table is
dropped -- yet it was reported as a blocker alongside a live queryset. At
the point the gate said "70 modules still read the legacy models", 21 of
those 70 contained no code reference at all. A gate that inflates its own
blocker count is a gate the reader stops believing, which is worse than no
gate.

So this walks the AST and separates three genuinely different things:

``code``
    A real runtime read: an import, a queryset, an ``isinstance`` check, a
    string model reference resolved at runtime. These are the true
    blockers. Each one has to be rewritten to go through Account/Website
    before the legacy tables can go, because each one raises at drop time.

``schema``
    A ``ForeignKey``/``OneToOneField`` declaration pointing at a legacy
    model. These are *not* fixed by rewriting a reader -- they are removed
    by the drop migration itself, which is the very thing the gate is
    deciding whether to allow. Counting them as blockers makes the gate
    unsatisfiable: it would demand the migration have already run before
    permitting the migration to run.

``prose``
    A mention in a comment or docstring. Never a blocker. Worth reporting
    only so the count is explainable -- otherwise the grep total and the
    gate total disagree and neither looks trustworthy.

Read-only. Imports nothing from the apps it scans, so it stays usable even
once those modules are mid-rewrite.
"""

import ast
import io
import pathlib
import tokenize

# Modules whose entire job is the transition: the models themselves, the
# parity/backfill machinery, the migration tooling. These legitimately name
# the legacy models forever, or at least until the drop lands.
ALLOWED_PREFIXES = (
    'clients/models.py',
    'clients/parity.py',
    'clients/account_setup.py',
    'clients/canonical_stamping.py',
    'clients/canonical_iteration.py',
    'clients/legacy_audit.py',
    'clients/signals.py',
    'clients/apps.py',
    'clients/account_models.py',
    'clients/management/commands/',
    'migrations/',
    'migrations_planned/',
)

SKIP_DIR_PARTS = ('myvenv', 'node_modules', '.git', 'staticfiles')

LEGACY_NAMES = frozenset({'ClientProfile', 'Project'})
RELATION_FIELDS = frozenset(
    {'ForeignKey', 'OneToOneField', 'ManyToManyField'})
# String forms Django resolves lazily -- `models.ForeignKey('clients.Project')`.
LEGACY_STRINGS = frozenset({
    'clients.ClientProfile', 'clients.Project', 'ClientProfile', 'Project',
})


class ModuleReport:
    """What one module does with the legacy models."""

    __slots__ = ('path', 'code_lines', 'schema_lines', 'prose_count')

    def __init__(self, path, code_lines, schema_lines, prose_count):
        self.path = path
        self.code_lines = code_lines
        self.schema_lines = schema_lines
        self.prose_count = prose_count

    @property
    def blocks_removal(self):
        """Only live code reads block the drop. See the module docstring."""
        return bool(self.code_lines)

    def __repr__(self):                                   # pragma: no cover
        return (f'<ModuleReport {self.path} code={len(self.code_lines)} '
                f'schema={len(self.schema_lines)} prose={self.prose_count}>')


def _docstring_line_span(tree):
    """Line numbers occupied by module/class/function docstrings."""
    lines = set()
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = getattr(first, 'end_lineno', first.lineno)
            lines.update(range(first.lineno, end + 1))
    return lines


def _schema_line_span(tree, source):
    """Line numbers inside a relation-field call naming a legacy model.

    Spans the whole call, not just its first line, because these are
    routinely written across several lines with `related_name` and
    `on_delete` below the model reference.
    """
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, 'attr', None) or getattr(func, 'id', None)
        if name not in RELATION_FIELDS:
            continue
        segment = ast.get_source_segment(source, node) or ''
        if any(legacy in segment for legacy in LEGACY_NAMES):
            end = getattr(node, 'end_lineno', node.lineno)
            lines.update(range(node.lineno, end + 1))
    return lines


def _references_legacy(node):
    if isinstance(node, ast.Name):
        return node.id in LEGACY_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in LEGACY_NAMES
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any(alias.name in LEGACY_NAMES for alias in node.names)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in LEGACY_STRINGS
    return False


def _count_prose(source, docstring_lines):
    """Comment lines plus docstring lines that name a legacy model."""
    total = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and any(
                    legacy in token.string for legacy in LEGACY_NAMES):
                total += 1
    except (tokenize.TokenError, IndentationError):
        # Malformed source: the AST pass already reported what it could.
        pass

    for number, text in enumerate(source.splitlines(), 1):
        if number in docstring_lines and any(
                legacy in text for legacy in LEGACY_NAMES):
            total += 1
    return total


def analyse_source(source, path='<string>'):
    """Classify one module's source. Returns a ModuleReport, or None if
    the file does not parse (a syntax error is the caller's problem, not
    a legacy-dependency finding)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    schema_lines = _schema_line_span(tree, source)
    docstring_lines = _docstring_line_span(tree)

    code_lines = set()
    for node in ast.walk(tree):
        if not _references_legacy(node):
            continue
        line = getattr(node, 'lineno', None)
        if line is None:
            continue
        # A relation declaration is schema; a docstring is prose. Neither
        # is a live read, so neither blocks.
        if line in schema_lines or line in docstring_lines:
            continue
        code_lines.add(line)

    return ModuleReport(
        path=path,
        code_lines=sorted(code_lines),
        schema_lines=sorted(schema_lines),
        prose_count=_count_prose(source, docstring_lines),
    )


def _is_scannable(relative):
    if any(part in relative for part in SKIP_DIR_PARTS):
        return False
    name = relative.rsplit('/', 1)[-1]
    if 'test' in name:
        return False
    return not any(allowed in relative for allowed in ALLOWED_PREFIXES)


def scan_repository(root='.'):
    """Every module outside the transition machinery that names a legacy
    model, ordered by how much real work it represents."""
    reports = []
    for path in sorted(pathlib.Path(root).rglob('*.py')):
        relative = str(path.relative_to(root)).replace('\\', '/')
        if not _is_scannable(relative):
            continue
        try:
            source = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if not any(legacy in source for legacy in LEGACY_NAMES):
            continue
        report = analyse_source(source, path=relative)
        if report is None:
            continue
        if report.code_lines or report.schema_lines or report.prose_count:
            reports.append(report)

    reports.sort(
        key=lambda r: (-len(r.code_lines), -len(r.schema_lines), r.path))
    return reports


def summarise(reports):
    """Totals for the readiness report."""
    blocking = [r for r in reports if r.blocks_removal]
    return {
        'modules': len(reports),
        'blocking_modules': len(blocking),
        'code_reads': sum(len(r.code_lines) for r in blocking),
        'schema_lines': sum(len(r.schema_lines) for r in reports),
        'prose_mentions': sum(r.prose_count for r in reports),
    }
