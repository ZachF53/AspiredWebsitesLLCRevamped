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
import re
import tokenize

# Modules whose entire job is the transition: the models themselves, the
# parity/backfill machinery, the migration tooling. These legitimately name
# the legacy models forever, or at least until the drop lands.
ALLOWED_PREFIXES = (
    'clients/models.py',
    'clients/parity.py',
    'clients/account_setup.py',
    'clients/canonical_stamping.py',
    'clients/legacy_teardown.py',
    'clients/legacy_audit.py',
    'clients/signals.py',
    'clients/apps.py',
    'clients/account_models.py',
    'clients/management/commands/',
    'migrations/',
    'migrations_planned/',
)

# Modules whose job is to REPORT on the cutover. They read the legacy
# models deliberately and are deleted by the drop rather than converted,
# so counting them as blockers makes the gate unsatisfiable in the same
# way FK declarations do — it would demand the removal have happened
# before permitting the removal.
#
# `admin_dashboard/data_health_views.py` renders the cutover-progress
# panel: legacy row counts, orphan counts, how much is left. Once the
# tables are gone that panel is describing something that no longer
# exists, and it comes out in the same change.
REPORTING_ON_THE_CUTOVER = (
    'admin_dashboard/data_health_views.py',
)

SKIP_DIR_PARTS = ('myvenv', 'node_modules', '.git', 'staticfiles')

LEGACY_NAMES = frozenset({'ClientProfile', 'Project'})

# Attributes that hold a legacy instance without naming its class.
#
# `request.client_profile` is set by clients.decorators.client_required and
# read by ~20 portal views. None of them mentions ClientProfile, so a
# name-based scan reports those modules as clean while they still break
# outright at drop time. Counting symbols is not the same as counting
# dependence, and this is where the two came apart -- the gate said
# clients/views.py had zero legacy reads while it had twenty.
#
# `legacy_client_profile` / `legacy_project` are the transitional FKs
# themselves. Phase 2 of the drop removes them, so any module reading one
# to decide what to do is depending on the very thing being removed --
# and, before that, is already taking a different code path for the
# canonical-only clients created since the cutover. `admin_dashboard`
# gated the stage-change email on `if legacy_cp is not None`, so those
# clients were never told their project had moved. None of those lines
# names ClientProfile, so a name-based scan scored the module clean.
LEGACY_ATTRIBUTES = frozenset({
    'client_profile', 'legacy_client_profile', 'legacy_project',
})
RELATION_FIELDS = frozenset(
    {'ForeignKey', 'OneToOneField', 'ManyToManyField'})
# String forms Django resolves lazily -- `models.ForeignKey('clients.Project')`.
LEGACY_STRINGS = frozenset({
    'clients.ClientProfile', 'clients.Project', 'ClientProfile', 'Project',
})


class ModuleReport:
    """What one module does with the legacy models."""

    __slots__ = ('path', 'code_lines', 'schema_lines', 'prose_count',
                 'reports_on_cutover')

    def __init__(self, path, code_lines, schema_lines, prose_count,
                 reports_on_cutover=False):
        self.path = path
        self.code_lines = code_lines
        self.schema_lines = schema_lines
        self.prose_count = prose_count
        self.reports_on_cutover = reports_on_cutover

    @property
    def blocks_removal(self):
        """Only live code reads block the drop. See the module docstring.

        A module that exists to REPORT on the cutover is excluded: it
        reads the legacy models on purpose and is deleted alongside them,
        so blocking on it would be circular.
        """
        return bool(self.code_lines) and not self.reports_on_cutover

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


#: Attributes that exist on ClientProfile/Project and on no canonical
#: model, used to identify a legacy row reached through a plain `.client`
#: hop. `firm_name` cannot be matched on its own -- `outreach.Lead` has
#: one too, and flagging those would be a false positive on code that has
#: nothing to do with the cutover. It is only conclusive *after* `.client`
#: or `.project`.
LEGACY_CHAIN_ATTRIBUTES = frozenset({'firm_name', 'migrated_account'})


def _is_legacy_chain(node):
    """``x.client.firm_name`` — a legacy read that names nothing legacy.

    The scan flags the identifier `ClientProfile` and the attribute
    `legacy_client_profile`; this shape contains neither. `client` is an
    ordinary attribute name and only the trailing attribute gives it
    away, which is why twenty-eight of these survived a gate reporting
    zero reads.

    The one that mattered was in `social.tasks.publish_due_posts`, inside
    an `except` block: `client` is None for every account created since
    the cutover, so the alert raised AttributeError, escaped the loop
    before `continue`, killed the task, and left every *other* client's
    scheduled posts unpublished.
    """
    if not isinstance(node, ast.Attribute):
        return False
    if node.attr not in LEGACY_CHAIN_ATTRIBUTES:
        return False
    inner = node.value
    return (isinstance(inner, ast.Attribute)
            and inner.attr in ('client', 'project'))


def _references_legacy(node):
    if isinstance(node, ast.Name):
        return node.id in LEGACY_NAMES
    if isinstance(node, ast.Attribute):
        return (node.attr in LEGACY_NAMES
                or node.attr in LEGACY_ATTRIBUTES
                or _is_legacy_chain(node))
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
        # The pre-filter has to admit the chain attributes too, or a
        # module whose only legacy read is `x.client.firm_name` is never
        # parsed at all. That is how `domains/views.py` escaped the scan
        # when `request.client_profile` was added -- the cheap substring
        # check ran before the AST pass and rejected the file.
        markers = LEGACY_NAMES | LEGACY_ATTRIBUTES | LEGACY_CHAIN_ATTRIBUTES
        if not any(marker in source for marker in markers):
            continue
        report = analyse_source(source, path=relative)
        if report is None:
            continue
        report.reports_on_cutover = any(
            marker in relative for marker in REPORTING_ON_THE_CUTOVER)
        if report.code_lines or report.schema_lines or report.prose_count:
            reports.append(report)

    reports.sort(
        key=lambda r: (-len(r.code_lines), -len(r.schema_lines), r.path))
    return reports


# ── Templates ───────────────────────────────────────────────────────────
#
# The scan above parses Python. Templates are not Python, so for the whole
# cutover they were invisible to it -- and twenty-two of them named the
# owner of a row through the legacy FK while the gate reported zero
# blockers. That is not a small gap: `{% url %}` with an empty argument
# raises NoReverseMatch and 500s the page, and `{{ }}` resolves a missing
# attribute to the empty string, which is worse, because it returns 200
# with the client's name silently missing.
#
# Those were found and fixed by hand. This is here so a twenty-third
# cannot be added quietly.

TEMPLATE_DEREF = re.compile(
    r'\b([a-z_][\w]*(?:\.[\w]+)*?\.(?:client|project))\.([\w]+)')
TEMPLATE_URL_TAG = re.compile(r'{%\s*url\b[^%]*%}')

#: Attributes that exist on ClientProfile/Project and on no canonical
#: model. Reading one through `.client` proves the variable is a legacy
#: row rather than a context variable that merely happens to be called
#: `client` -- `admin_dashboard/clients_onboarding` builds
#: `{'client': <Website>}` dicts, and rewriting those would be the bug.
TEMPLATE_LEGACY_ONLY_ATTRS = frozenset({
    'firm_name', 'migrated_account', 'legacy_client_profile',
})

#: Templates whose job is to report on the cutover.
TEMPLATE_EXEMPT = ('data_health.html', 'account_detail.html')


class TemplateFinding:
    """One template variable that reaches a row through the legacy FK."""

    __slots__ = ('path', 'variable', 'lines', 'in_url_tag')

    def __init__(self, path, variable, lines, in_url_tag):
        self.path = path
        self.variable = variable
        self.lines = lines
        self.in_url_tag = in_url_tag

    @property
    def severity(self):
        """`url` = a 500. `display` = a 200 with the value missing."""
        return 'url' if self.in_url_tag else 'display'

    def __repr__(self):                                   # pragma: no cover
        return (f'<TemplateFinding {self.path} {self.variable} '
                f'{self.severity}>')


def scan_templates(root='.'):
    """Templates still resolving a row's owner through the legacy FK.

    Groups by (template, variable) so a variable proven legacy by one
    line is reported for all of its uses -- including the bare `.id`
    ones inside url tags, which carry no evidence of their own and are
    the ones that 500.
    """
    grouped = {}

    for path in sorted(pathlib.Path(root).rglob('*.html')):
        relative = str(path.relative_to(root)).replace('\\', '/')
        if any(part in relative for part in SKIP_DIR_PARTS):
            continue
        if any(name in relative for name in TEMPLATE_EXEMPT):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if '.client.' not in text and '.project.' not in text:
            continue

        for number, line in enumerate(text.splitlines(), 1):
            spans = [(t.start(), t.end())
                     for t in TEMPLATE_URL_TAG.finditer(line)]
            for match in TEMPLATE_DEREF.finditer(line):
                variable, attr = match.group(1), match.group(2)
                if variable.startswith('form.'):
                    continue            # a bound form field, not a relation
                in_url = any(a <= match.start() < b for a, b in spans)
                grouped.setdefault((relative, variable), []).append(
                    (number, attr, in_url))

    findings = []
    for (relative, variable), uses in grouped.items():
        attrs = {attr for _, attr, _ in uses}
        if not (attrs & TEMPLATE_LEGACY_ONLY_ATTRS):
            continue
        findings.append(TemplateFinding(
            path=relative,
            variable=variable,
            lines=sorted({n for n, _, _ in uses}),
            in_url_tag=any(u for _, _, u in uses),
        ))

    findings.sort(key=lambda f: (not f.in_url_tag, f.path, f.variable))
    return findings


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
