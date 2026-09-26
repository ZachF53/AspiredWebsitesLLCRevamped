"""
File-integrity monitoring for sites on Aspired-managed Droplets.

Run as one step of the SSH droplet health audit
(reporting.droplet_audit). Over the already-open automation SSH
session it sha256-hashes the site's application code, then compares the
result with the site's newest ``FileIntegrityBaseline``:

- no baseline yet  -> the current manifest is stored as the baseline
                      and the run reports "baseline recorded";
- baseline exists  -> added / removed / changed files are reported.

What gets hashed:

- custom builds: every file under /var/www EXCEPT user uploads,
  collected static, logs, virtualenvs, bytecode caches, VCS metadata
  and databases — i.e. the code we deployed, not data that changes by
  itself;
- WordPress: wp-admin/, wp-includes/, the PHP files under
  wp-content/plugins/ and wp-content/themes/, and the top-level PHP
  files (index.php, wp-config.php, …) — where injected backdoors live.

Everything that touches the network is in ``run_file_integrity``; the
command builder, output parser and diff are pure functions so they are
unit-testable without SSH.
"""

import logging
import shlex

logger = logging.getLogger(__name__)

CUSTOM_ROOT = '/var/www'

# Directory names pruned from the custom-build walk.
EXCLUDED_DIR_NAMES = (
    'media', 'private_media', 'static', 'staticfiles',
    'logs', 'log',
    'venv', '.venv', 'myvenv', 'env', 'virtualenv', 'site-packages',
    '__pycache__', '.git', 'node_modules', '.cache',
)
# File patterns skipped everywhere (change on their own at runtime).
EXCLUDED_FILE_PATTERNS = (
    '*.pyc', '*.log', '*.sqlite3', '*.sqlite3-journal', '*.pid', '*.sock',
)

# Hard cap on files hashed per run — keeps a runaway tree (someone
# unpacked a backup under /var/www) from producing a multi-MB manifest.
MAX_FILES = 20000

# How many paths per category are kept in the stored diff / shown.
DIFF_LIST_LIMIT = 200


def _name_tests(names, flag='-name'):
    return ' -o '.join(f'{flag} {shlex.quote(n)}' for n in names)


def build_manifest_command(platform, root=None):
    """Shell command that prints ``<sha256>  <path>`` lines for every
    file in scope for `platform` ('custom' or 'wordpress'). `root` is
    the WordPress install directory (ignored for custom builds)."""
    file_excludes = ' '.join(
        f'! -name {shlex.quote(p)}' for p in EXCLUDED_FILE_PATTERNS)
    tail = (f' 2>/dev/null | head -z -n {MAX_FILES} '
            f'| xargs -0 -r sha256sum 2>/dev/null')

    if platform == 'wordpress':
        r = shlex.quote((root or '').rstrip('/') or '/var/www/html')
        return (
            '{ '
            f'find {r}/wp-admin {r}/wp-includes -type f {file_excludes} '
            '-print0 2>/dev/null; '
            f'find {r}/wp-content/plugins {r}/wp-content/themes -type f '
            "-name '*.php' -print0 2>/dev/null; "
            f"find {r} -maxdepth 1 -type f -name '*.php' -print0 "
            '2>/dev/null; '
            '}' + tail
        )

    prune = _name_tests(EXCLUDED_DIR_NAMES)
    return (
        f'find {CUSTOM_ROOT} \\( -type d \\( {prune} \\) -prune \\) -o '
        f'\\( -type f {file_excludes} -print0 \\)' + tail
    )


def parse_sha256sum_output(text):
    """Parse ``sha256sum`` output into ``{path: hexdigest}``. Accepts
    both text-mode (two spaces) and binary-mode (`` *``) separators and
    ignores anything that isn't a well-formed line."""
    manifest = {}
    for line in (text or '').splitlines():
        line = line.rstrip('\r')
        if len(line) < 67:
            continue
        digest, sep, path = line[:64], line[64:66], line[66:]
        if sep not in ('  ', ' *'):
            continue
        digest = digest.lower()
        if not all(c in '0123456789abcdef' for c in digest):
            continue
        if path:
            manifest[path] = digest
    return manifest


def diff_manifests(baseline, current):
    """Compare two ``{path: sha256}`` manifests.

    Returns ``{'added': [...], 'removed': [...], 'changed': [...]}``,
    each sorted — added = in current only, removed = in baseline only,
    changed = in both with a different hash.
    """
    baseline = baseline or {}
    current = current or {}
    b_keys, c_keys = set(baseline), set(current)
    return {
        'added': sorted(c_keys - b_keys),
        'removed': sorted(b_keys - c_keys),
        'changed': sorted(p for p in (b_keys & c_keys)
                          if baseline[p] != current[p]),
    }


def diff_change_count(diff):
    return sum(len(diff.get(k) or []) for k in ('added', 'removed', 'changed'))


def _truncate_diff(diff, limit=DIFF_LIST_LIMIT):
    return {k: list(v)[:limit] for k, v in diff.items()}


def _discover_wordpress_root(ssh):
    from vault.ssh_ops import run_remote
    code, out, _ = run_remote(
        ssh,
        'find /var/www -maxdepth 4 -type f -name wp-config.php '
        '2>/dev/null | head -n 1',
        check=False)
    path = (out or '').strip()
    if code != 0 or not path.endswith('/wp-config.php'):
        return None
    return path[:-len('/wp-config.php')]


def run_file_integrity(ssh, website, check=None):
    """Hash the site's code over `ssh` and diff against its baseline.

    Returns ``(raw, changes_count)`` where `raw` is stored on
    ``DropletHealthCheck.raw_file_integrity``. ``changes_count`` is None
    when the step was skipped, 0 when the baseline was just recorded or
    nothing changed. Never raises for a remote-side problem — a failed
    hash run is recorded as skipped.
    """
    from vault.ssh_ops import run_remote

    from reporting.models import FileIntegrityBaseline

    platform = ('wordpress' if website.build_platform == 'wordpress'
                else 'custom')
    root = CUSTOM_ROOT
    if platform == 'wordpress':
        root = _discover_wordpress_root(ssh)
        if not root:
            return {'status': 'skipped',
                    'reason': 'WordPress install not found under /var/www'}, None

    cmd = build_manifest_command(platform, root)
    code, out, err = run_remote(ssh, cmd, check=False, timeout=180)
    current = parse_sha256sum_output(out)
    if not current:
        return {'status': 'skipped',
                'reason': 'no files could be hashed',
                'exit_code': code, 'stderr': (err or '')[:1000]}, None

    raw = {
        'platform': platform,
        'root': root,
        'file_count': len(current),
        'truncated': len(current) >= MAX_FILES,
        'manifest': current,
    }

    baseline = (FileIntegrityBaseline.objects
                .filter(website=website).order_by('-created_at').first())
    if baseline is None:
        FileIntegrityBaseline.objects.create(
            website=website, manifest=current, file_count=len(current),
            source_check=check)
        raw.update({'status': 'baseline_recorded',
                    'added': [], 'removed': [], 'changed': [],
                    'added_count': 0, 'removed_count': 0,
                    'changed_count': 0})
        return raw, 0

    diff = diff_manifests(baseline.manifest, current)
    count = diff_change_count(diff)
    raw.update(_truncate_diff(diff))
    raw.update({
        'status': 'changed' if count else 'clean',
        'baseline_id': str(baseline.id),
        'baseline_created_at': baseline.created_at.isoformat(),
        'added_count': len(diff['added']),
        'removed_count': len(diff['removed']),
        'changed_count': len(diff['changed']),
    })
    return raw, count


def accept_check_as_baseline(check, accepted_by=''):
    """Store `check`'s current manifest as the site's new baseline.
    Returns the new FileIntegrityBaseline, or None when the check has
    no manifest to accept."""
    from reporting.models import FileIntegrityBaseline

    manifest = (check.raw_file_integrity or {}).get('manifest')
    if not manifest or check.website_new_id is None:
        return None
    return FileIntegrityBaseline.objects.create(
        website_id=check.website_new_id, manifest=manifest,
        file_count=len(manifest), source_check=check,
        accepted_by=(accepted_by or '')[:150])
