"""
Build ``core/static/css/public.css`` — the public-site CSS bundle.

Why this exists
---------------
``main.css`` is one ~430 KB stylesheet shared by the public marketing
site, the client portal, AND the admin dashboard. Every anonymous
visitor to the homepage was downloading the entire admin UI's styles.
PageSpeed attributed ~2,200 ms of unused CSS to it and mobile LCP sat
at 4.1 s against a 2.0 s budget (Master Plan §5.2).

This command derives a public-only bundle from ``main.css`` so the
marketing site ships roughly a fifth of the bytes.

How it decides
--------------
A CSS block is kept when it is *not* class-scoped (``:root`` tokens,
resets, element selectors, keyframes, font-face — these must always
ship) **or** when any class in its selector is actually referenced by
the public template tree or by public JavaScript. ``@media`` /
``@supports`` groups are filtered recursively and dropped if nothing
inside survives.

The bias is deliberately toward keeping: an unused rule costs a few
bytes, a missing one breaks the page.

``main.css`` is never modified — the portal and admin dashboard keep
loading it in full, so this change cannot affect them.

Usage
-----
    python manage.py build_public_css           # write the bundle
    python manage.py build_public_css --check   # CI: fail if stale

``core.tests.PublicCssBundleTests`` runs ``--check`` logic so the
bundle cannot silently drift out of date after a ``main.css`` edit.
"""
from __future__ import annotations

import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

CSS_DIR = os.path.join(settings.BASE_DIR, 'core', 'static', 'css')
SOURCE = os.path.join(CSS_DIR, 'main.css')
TARGET = os.path.join(CSS_DIR, 'public.css')

# Template trees whose pages extend core/templates/base.html.
PUBLIC_TEMPLATE_ROOTS = [
    ('public', 'templates'),
    ('core', 'templates'),
    ('scheduler', 'templates'),
    ('billing', 'templates'),
    ('onboarding', 'templates'),
    ('sync', 'templates'),
]
PUBLIC_EXTRA_TEMPLATES = [
    ('clients', 'templates', 'clients', 'contract_pay.html'),
    ('clients', 'templates', 'clients', 'contract_sign.html'),
    ('clients', 'templates', 'clients', 'contract_signed.html'),
]
# JS loaded by the public base template — classes it toggles at runtime
# never appear in the HTML source, so scan these too.
PUBLIC_JS = ['main.js', 'input_masks.js', 'aspired-tracker.js']

HEADER = """/* ============================================================
   public.css — GENERATED FILE. DO NOT EDIT BY HAND.

   Public-site subset of main.css, produced by:
       python manage.py build_public_css

   Edit main.css and re-run the command. `manage.py
   build_public_css --check` (and the core test suite) will fail if
   this file is stale.
   ============================================================ */
"""

_CLASS_RE = re.compile(r'\.(-?[A-Za-z_][A-Za-z0-9_-]*)')
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]*$')
_GROUP_AT_RULE = re.compile(r'^\s*@(media|supports|layer|container)\b', re.I)


def _tokenise_classes(value: str) -> set[str]:
    """
    Pull class names out of a template ``class="..."`` value.

    Templates build modifier classes dynamically:

        class="score-card score-card--{{ card.status }}"

    Splitting on ``{`` leaves the stub ``score-card--``. Those stubs are
    returned with their trailing hyphen intact and treated as *prefixes*
    by :func:`_keep_rule`, so ``.score-card--good``, ``--warn`` and
    ``--fail`` are all kept even though no template contains those exact
    strings. Without this, dynamically-themed components lose their
    styling and fail open — silently, and only for some data values.
    """
    found = set()
    for token in re.split(r'[\s{}%<>|\'"()]+', value):
        token = token.strip()
        if token and _IDENT_RE.match(token):
            found.add(token)
    return found


def collect_public_classes() -> set[str]:
    """Every class name the public site could plausibly apply."""
    used: set[str] = set()
    paths: list[str] = []

    for parts in PUBLIC_TEMPLATE_ROOTS:
        root = os.path.join(settings.BASE_DIR, *parts)
        for dirpath, _dirnames, filenames in os.walk(root):
            paths.extend(os.path.join(dirpath, f) for f in filenames
                         if f.endswith('.html'))
    for parts in PUBLIC_EXTRA_TEMPLATES:
        path = os.path.join(settings.BASE_DIR, *parts)
        if os.path.exists(path):
            paths.append(path)
    for name in PUBLIC_JS:
        path = os.path.join(CSS_DIR, '..', 'js', name)
        if os.path.exists(path):
            paths.append(os.path.normpath(path))

    for path in paths:
        with open(path, encoding='utf-8', errors='ignore') as handle:
            text = handle.read()
        if path.endswith('.html'):
            for match in re.finditer(r'class\s*=\s*"([^"]*)"', text):
                used |= _tokenise_classes(match.group(1))
            for match in re.finditer(r"class\s*=\s*'([^']*)'", text):
                used |= _tokenise_classes(match.group(1))
        else:
            # JS: classList.add('x'), className = 'x y', querySelector('.x')
            for match in re.finditer(
                    r"""classList\.\w+\(\s*['"]([^'"]+)""", text):
                used |= _tokenise_classes(match.group(1))
            for match in re.finditer(
                    r"""className\s*=\s*['"]([^'"]*)""", text):
                used |= _tokenise_classes(match.group(1))
            for match in re.finditer(
                    r"""querySelector(?:All)?\(\s*['"]([^'"]+)""", text):
                used |= set(_CLASS_RE.findall(match.group(1)))
    return used


def _split_blocks(css: str) -> list[tuple[str, str]]:
    """Split CSS into ('comment' | 'ws' | 'rule', text) top-level blocks."""
    blocks: list[tuple[str, str]] = []
    i, n = 0, len(css)
    while i < n:
        if css.startswith('/*', i):
            end = css.find('*/', i + 2)
            end = n if end == -1 else end + 2
            blocks.append(('comment', css[i:end]))
            i = end
        elif css[i] in ' \t\r\n':
            end = i
            while end < n and css[end] in ' \t\r\n':
                end += 1
            blocks.append(('ws', css[i:end]))
            i = end
        else:
            brace = css.find('{', i)
            if brace == -1:
                blocks.append(('rule', css[i:]))
                break
            depth, end = 0, brace
            while end < n:
                if css[end] == '{':
                    depth += 1
                elif css[end] == '}':
                    depth -= 1
                    if depth == 0:
                        end += 1
                        break
                end += 1
            blocks.append(('rule', css[i:end]))
            i = end
    return blocks


def _keep_rule(selector: str, public_classes: set[str]) -> bool:
    names = set(_CLASS_RE.findall(selector))
    if not names:
        return True          # element / :root / keyframes / reset
    if names & public_classes:
        return True
    # Prefix stubs from dynamically-built modifiers — see
    # _tokenise_classes. Only stubs ending in '-' are prefixes; a bare
    # class name never matches this way.
    prefixes = tuple(c for c in public_classes if c.endswith('-'))
    return any(name.startswith(prefixes) for name in names) if prefixes \
        else False


def _filter(css: str, public_classes: set[str], depth: int = 0) -> str:
    """Return the public-relevant subset of a CSS string."""
    out: list[str] = []
    for kind, text in _split_blocks(css):
        if kind in ('ws', 'comment'):
            # Keep newlines for readability; drop comment bodies at depth
            # 0 only if the following rule is dropped (handled by the
            # simple approach of always keeping comments).
            out.append(text if kind == 'ws' else text)
            continue

        brace = text.find('{')
        if brace == -1:
            out.append(text)
            continue
        selector = text[:brace]
        body = text[brace + 1:text.rfind('}')]

        if _GROUP_AT_RULE.match(selector):
            inner = _filter(body, public_classes, depth + 1)
            if inner.strip():
                out.append(f'{selector}{{{inner}}}')
            continue

        # @font-face, @keyframes, @page, @charset, @import: always keep.
        if selector.lstrip().startswith('@'):
            out.append(text)
            continue

        if _keep_rule(selector, public_classes):
            out.append(text)
    return ''.join(out)


def build() -> str:
    with open(SOURCE, encoding='utf-8') as handle:
        css = handle.read()
    public_classes = collect_public_classes()
    body = _filter(css, public_classes)
    # Collapse the runs of blank lines left where rules were removed.
    body = re.sub(r'\n{3,}', '\n\n', body)
    return HEADER + body.strip() + '\n'


class Command(BaseCommand):
    help = 'Generate core/static/css/public.css from main.css.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--check', action='store_true',
            help='Exit non-zero if public.css is missing or stale.')

    def handle(self, *args, **options):
        generated = build()

        if options['check']:
            if not os.path.exists(TARGET):
                raise CommandError(
                    'public.css is missing. Run: '
                    'python manage.py build_public_css')
            with open(TARGET, encoding='utf-8') as handle:
                current = handle.read()
            if current != generated:
                raise CommandError(
                    'public.css is stale (main.css changed). Run: '
                    'python manage.py build_public_css')
            self.stdout.write(self.style.SUCCESS('public.css is current.'))
            return

        with open(SOURCE, encoding='utf-8') as handle:
            before = len(handle.read())
        with open(TARGET, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(generated)
        after = len(generated)
        self.stdout.write(self.style.SUCCESS(
            f'public.css written: {after:,} bytes '
            f'(from {before:,} — {100 - after * 100 // before}% smaller)'))
