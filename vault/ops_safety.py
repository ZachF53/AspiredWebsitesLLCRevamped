"""
AI Ops Agent — safety gate.

Defines the dangerous-command pattern table and the regex helpers used
by `ops_chat` / `ops_execute` to gate human-only commands.

The gate is intentionally conservative: every match here forces a
human-approval step before execution. False positives are cheap (one
extra click); false negatives could trash a production box. When in
doubt, ADD to the table.

This module has zero Django imports so it can be unit-tested directly
and imported by both the synchronous chat view and the Channels SSH
consumer without booting the app registry.
"""

import re

# (regex, human-readable reason). Order matters only for the first hit
# that wins; in practice the patterns are narrow enough that overlap is
# rare. Patterns are anchored with \b where the surrounding context
# would otherwise let things like "shutdown_helper.py" trip the gate.
DANGEROUS_PATTERNS = [
    (r'\brm\s+-rf?\b',          'File deletion (rm -r / -rf)'),
    (r'\brmdir\b',              'Directory removal (rmdir)'),
    (r'\bDROP\s+TABLE\b',       'Database table drop'),
    (r'\bDROP\s+DATABASE\b',    'Database drop'),
    (r'\bTRUNCATE\b',           'Database truncate'),
    (r'\bdelete\s+from\b',      'Database delete'),
    (r'\bufw\b',                'Firewall change (ufw)'),
    (r'\biptables\b',           'Firewall change (iptables)'),
    (r'supervisorctl\s+stop\b', 'Service stop (supervisorctl)'),
    (r'systemctl\s+stop\b',     'Service stop (systemctl)'),
    (r'systemctl\s+disable\b',  'Service disable'),
    (r'\bpoweroff\b',           'Server power-off'),
    (r'\breboot\b',             'Server reboot'),
    (r'\bshutdown\b',           'Server shutdown'),
    (r'chmod\s+777',            'Dangerous permissions (chmod 777)'),
    (r'chown\s+-R\s+root',      'Recursive root ownership change'),
    (r'>\s*/var/www',           'Overwrite web files (> /var/www…)'),
    (r'\bdd\s+if=',             'Raw disk write (dd)'),
    (r'mkfs\.',                 'Filesystem format (mkfs)'),
    (r'\bcrontab\s+-r\b',       'Crontab deletion (crontab -r)'),
    (r'>\s*/etc/',              'Overwrite system config (> /etc/…)'),
    (r'\bpasswd\b',             'Password change'),
    (r'\bsudo\s+su\b',          'Root escalation (sudo su)'),
]


def check_command_safety(command: str) -> dict:
    """
    Test `command` against the dangerous-pattern table.

    Returns a dict that is ALWAYS shaped the same:

        {'is_dangerous': bool,
         'reason':     str | None,
         'pattern':    str | None}

    Callers must not raise — they should surface `reason` to the human
    when `is_dangerous` is True.
    """
    if not command:
        return {'is_dangerous': False, 'reason': None, 'pattern': None}
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return {
                'is_dangerous': True,
                'reason': reason,
                'pattern': pattern,
            }
    return {'is_dangerous': False, 'reason': None, 'pattern': None}


# Commands the agent might mention with a single backtick that are
# safely runnable (read-only diagnostics, mostly). Used only as the
# fallback when no ```bash block was found in the reply.
_INLINE_COMMAND_PREFIXES = (
    'sudo', 'systemctl', 'supervisorctl', 'tail', 'cat', 'grep',
    'df', 'free', 'uptime', 'ps', 'netstat', 'nginx', 'python',
    'pip', 'apt', 'service', 'journalctl', 'ls', 'who', 'w',
    'top', 'htop', 'ss', 'curl', 'wget', 'whoami', 'hostname',
    'id', 'date', 'env',
)


def extract_commands_from_response(text: str) -> list[str]:
    """
    Pull shell commands out of an agent reply.

    Priority order (so the agent can be increasingly explicit when it
    matters):

      1. Fenced ```bash / ```sh / ```shell blocks → every non-comment
         line is a command.
      2. Bare ``` blocks → same treatment (Claude often drops the
         language tag).
      3. `$ command` lines anywhere in the text.
      4. ONLY if 1-3 found nothing: single-backtick code spans that
         start with a known-safe diagnostic prefix.

    Returns a list of command strings, in the order they appeared.
    Empty list when nothing matched.
    """
    if not text:
        return []

    commands: list[str] = []

    # 1 + 2 — fenced code blocks (optionally tagged bash/sh/shell).
    fence_re = re.compile(
        r'```(?:bash|sh|shell)?\s*\n(.*?)```',
        re.DOTALL | re.IGNORECASE,
    )
    for block in fence_re.findall(text):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            commands.append(line)

    # 3 — `$ command` prompt lines.
    for line in re.findall(r'^\$\s+(.+)$', text, flags=re.MULTILINE):
        commands.append(line.strip())

    if commands:
        return commands

    # 4 — fallback: inline `cmd` spans that look like a real diagnostic.
    for span in re.findall(r'`([^`\n]+)`', text):
        first_token = span.strip().split(' ', 1)[0]
        if first_token in _INLINE_COMMAND_PREFIXES:
            commands.append(span.strip())

    return commands
