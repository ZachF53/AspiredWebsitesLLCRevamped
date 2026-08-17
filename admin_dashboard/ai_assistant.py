"""
Phase 4 — Admin AI Assistant.

Natural-language command interface for the admin dashboard.
"Move Johnson Law to Design" → parsed intent → preview → confirm →
execute via clients.services.

Pipeline:
  parse_command(text)         → {intent, args}  or  {clarify: '...'}
  resolve_client(name)        → ClientProfile  or  list (ambiguous)
                              → raises if no match
  build_preview(intent, args) → dict for the preview card UI
  execute(intent, args, set_by)
                              → calls clients.services.*  →  result dict

Every command also writes an AIAssistantLog row (Phase 4.5).
"""

from difflib import SequenceMatcher

from clients.services import (
    GuardError,
    add_revision,
    approve_staging,
    change_client_stage,
    create_out_of_scope_invoice,
    get_client_status,
    mark_intake_complete,
    mark_live,
)
from reporting.ai import AIError, AINotConfigured, claude_tools


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema — one per supported intent. Names match service functions.
# ─────────────────────────────────────────────────────────────────────────────
# These are sent to Claude verbatim; the model picks one and returns
# `{name, input}`. Keep descriptions short and precise — the model uses
# them to disambiguate.
TOOLS = [
    {
        'name': 'move_stage',
        'description': (
            'Move a client project to a specific stage. Stages are: '
            'intake, structure, design, content, review, revisions, '
            'pre_launch, live. Use this for natural phrases like '
            '"move X to design", "advance X to review".'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {
                    'type': 'string',
                    'description': 'Client name or firm (will be fuzzy-matched).'},
                'stage': {
                    'type': 'string',
                    'enum': ['intake', 'structure', 'design', 'content',
                             'review', 'revisions', 'pre_launch', 'live'],
                    'description': 'Target stage.'},
                'note': {
                    'type': 'string',
                    'description': 'Optional admin note on the transition.'},
            },
            'required': ['client', 'stage'],
        },
    },
    {
        'name': 'mark_intake_complete',
        'description': (
            'Mark a client\'s intake form as complete so the project can '
            'advance past intake. Use for "mark X intake done", "X intake '
            'complete", "intake finished for X".'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {'type': 'string'},
            },
            'required': ['client'],
        },
    },
    {
        'name': 'approve_staging',
        'description': (
            'Client approved the staging site — move them to pre_launch. '
            'Use for "X approved staging", "X signed off on the design".'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {'type': 'string'},
            },
            'required': ['client'],
        },
    },
    {
        'name': 'mark_live',
        'description': (
            'Launch a client — move them to "live" stage. Requires final '
            'payment to have cleared. Use for "X is live", "launch X", '
            '"X is now live".'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {'type': 'string'},
            },
            'required': ['client'],
        },
    },
    {
        'name': 'add_revision',
        'description': (
            'Record a revision request for a client. Use for "add a '
            'revision for X — change the header colour" etc. Defaults '
            'to is_major=True; pass is_major=false for minor changes '
            'that don\'t count against the limit.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {'type': 'string'},
                'description': {'type': 'string'},
                'is_major': {'type': 'boolean'},
            },
            'required': ['client', 'description'],
        },
    },
    {
        'name': 'create_out_of_scope_invoice',
        'description': (
            'Create a pending MiniInvoice for ad-hoc out-of-scope work '
            '(NOT a revision — use add_revision for revisions). Use for '
            '"X needs an extra practice area page", "bill X $200 for the '
            'extra logo work".'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {'type': 'string'},
                'description': {'type': 'string'},
                'amount': {
                    'type': 'number',
                    'description': 'Dollar amount, must be > 0.'},
                'hours': {'type': 'number'},
            },
            'required': ['client', 'description', 'amount'],
        },
    },
    {
        'name': 'get_status',
        'description': (
            'Read-only — get the current state of a client (stage, '
            'payment, revisions, etc). Use for "what stage is X", "show '
            'me X", "how is X doing".'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'client': {'type': 'string'},
            },
            'required': ['client'],
        },
    },
]


SYSTEM_PROMPT = (
    'You are an admin assistant for a web-design agency (Aspired '
    'Websites LLC). The operator types short natural-language '
    'commands; you map them to ONE tool call from the toolset. '
    'NEVER answer in prose if a tool fits — call the tool. Only '
    'answer in text if the request is ambiguous or unrelated to '
    'the available tools (then ask a clarifying question).'
)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Parse
# ─────────────────────────────────────────────────────────────────────────────

def parse_command(text):
    """Return {'intent': str, 'args': dict} for a tool-use response, OR
    {'clarify': str} when Claude returned a text reply asking for
    clarification, OR raises AINotConfigured / AIError."""
    text = (text or '').strip()
    if not text:
        return {'clarify': 'Type a command, e.g. "Move Johnson Law to design".'}

    result = claude_tools(
        messages=[{'role': 'user', 'content': text}],
        tools=TOOLS,
        system=SYSTEM_PROMPT,
    )
    if result.get('kind') == 'tool_use':
        return {'intent': result['name'], 'args': result.get('input', {})}
    return {'clarify': result.get('text') or
            'I couldn\'t map that to a known action. Try rephrasing.'}


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Resolve client name → ClientProfile
# ─────────────────────────────────────────────────────────────────────────────

FUZZY_THRESHOLD = 0.6  # Looser than dedup (0.8) because admins type partials


class ClientNotFound(Exception):
    """No client matched a fuzzy-search query."""


class ClientAmbiguous(Exception):
    """≥2 plausible matches — operator must pick one. .matches holds them."""

    def __init__(self, matches):
        self.matches = matches
        super().__init__(
            f'Multiple matches: ' +
            ', '.join(f'"{m.name}"' for m in matches[:5]))


def resolve_client(name_query):
    """Fuzzy-match a name fragment against active Website names.

    Matches SITES, not accounts. The commands this feeds ("move X to
    design", "X is live") all change a build's state, and a build is a
    site — on a two-site account, naming the account cannot say which one
    the operator meant. Matching sites makes the ambiguity visible: the
    operator is asked which, instead of the wrong site being moved.

    Strategy:
      1. Exact case-insensitive match wins immediately.
      2. name CONTAINS the query (case-insensitive) — if exactly one,
         use it.
      3. SequenceMatcher ratio ≥ FUZZY_THRESHOLD — top 1 if uniquely best,
         otherwise raise ClientAmbiguous with all matches.
      4. Nothing → ClientNotFound.

    Operates on active sites under active accounts.
    """
    from clients.account_models import Website

    query = (name_query or '').strip()
    if not query:
        raise ClientNotFound('No client name in the command.')

    qs = Website.objects.select_related('account')
    # Filter to active when the column exists. Don't error if the
    # status field was renamed/removed in a future migration.
    try:
        qs = qs.filter(status='active') | qs.filter(status='')
    except Exception:
        pass

    # 1) Exact (case-insensitive)
    exact = list(qs.filter(name__iexact=query)[:2])
    if len(exact) == 1:
        return exact[0]
    if len(exact) >= 2:
        raise ClientAmbiguous(exact)

    # 2) Contains — on the site name OR its account's, so "move Vance to
    #    design" still finds both Vance sites and asks which.
    from django.db.models import Q

    contains = list(qs.filter(
        Q(name__icontains=query) | Q(account__name__icontains=query))[:10])
    if len(contains) == 1:
        return contains[0]
    if len(contains) >= 2:
        # Multiple firms contain the query string verbatim — definitely
        # ambiguous. Don't fall through to fuzzy: the operator typed
        # too short of a query.
        raise ClientAmbiguous(contains)

    # 3) Fuzzy
    q_lower = query.lower()
    scored = []
    pool = contains if contains else list(qs[:500])
    for cp in pool:
        ratio = SequenceMatcher(
            None, q_lower, (cp.name or '').lower()).ratio()
        if ratio >= FUZZY_THRESHOLD:
            scored.append((ratio, cp))
    scored.sort(key=lambda t: -t[0])
    if not scored:
        raise ClientNotFound(
            f'No client matching "{query}". Try a longer name.')
    # Unique winner if there's a clear ratio gap (>=0.05).
    if len(scored) == 1 or (scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1]
    # Otherwise ambiguous — return all top-scorers (within 0.05 of best).
    top_score = scored[0][0]
    near = [cp for s, cp in scored if top_score - s < 0.05]
    raise ClientAmbiguous(near)


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Preview (no mutation)
# ─────────────────────────────────────────────────────────────────────────────

def build_preview(intent, args, profile):
    """Return a dict the UI renders before the operator confirms.
    Includes the current state + a one-line summary of what will
    happen + any guard warnings (e.g. mark_live when not fully_paid)."""
    state = get_client_status(profile)
    summary = _summarise(intent, args, profile)
    warnings = _preflight_warnings(intent, args, profile)
    return {
        'intent': intent,
        'args': args,
        'client_id': str(profile.id),
        'firm_name': profile.name,
        'state': state,
        'summary': summary,
        'warnings': warnings,
        'blocked': any(w.get('blocked') for w in warnings),
    }


def _summarise(intent, args, profile):
    if intent == 'move_stage':
        return (f'Move {profile.name} from "{profile.stage}" to '
                f'"{args.get("stage")}".')
    if intent == 'mark_intake_complete':
        return f'Mark {profile.name} intake as complete.'
    if intent == 'approve_staging':
        return (f'Move {profile.name} to "pre_launch" '
                f'(staging approved).')
    if intent == 'mark_live':
        return f'Launch {profile.name} — set stage to "live".'
    if intent == 'add_revision':
        flavour = 'major' if args.get('is_major', True) else 'minor'
        return (f'Add a {flavour} revision for {profile.name}: '
                f'"{(args.get("description") or "")[:80]}".')
    if intent == 'create_out_of_scope_invoice':
        return (f'Create a pending MiniInvoice for {profile.name} — '
                f'${args.get("amount")} for '
                f'"{(args.get("description") or "")[:60]}".')
    if intent == 'get_status':
        return f'Show {profile.name}\'s current state (read-only).'
    return f'{intent}({args})'


def _preflight_warnings(intent, args, profile):
    """Surface guards as inline warnings so the operator knows BEFORE
    they click confirm. Each item: {'text': str, 'blocked': bool}."""
    warnings = []
    state = get_client_status(profile)
    if intent in ('mark_live',) and not state['payment_status'] == 'fully_paid':
        warnings.append({
            'text': (f'Final payment NOT cleared (payment_status='
                     f'{state["payment_status"]}). This action will be '
                     f'REFUSED.'),
            'blocked': True,
        })
    if intent == 'add_revision' and state['has_unpaid_out_of_scope']:
        warnings.append({
            'text': ('Unpaid out-of-scope MiniInvoice on file — new '
                     'revisions are blocked until it clears.'),
            'blocked': True,
        })
    if (intent == 'add_revision' and args.get('is_major', True)
            and state['over_revision_limit']):
        warnings.append({
            'text': ('This major revision will exceed the included '
                     'revision limit — an out-of-scope MiniInvoice will '
                     'be auto-created.'),
            'blocked': False,
        })
    if intent == 'create_out_of_scope_invoice':
        try:
            amt = float(args.get('amount', 0))
        except (TypeError, ValueError):
            amt = 0
        if amt <= 0:
            warnings.append({
                'text': 'Amount must be > 0 — this action will be REFUSED.',
                'blocked': True,
            })
    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Execute (after operator confirms)
# ─────────────────────────────────────────────────────────────────────────────

def execute(intent, args, profile, *, set_by='AI assistant'):
    """Run the service function for `intent`. Returns {'ok': bool,
    'message': str, 'extra': any}. Never raises ValueError/GuardError —
    those are caught and converted to {'ok': False, 'message': ...}."""
    try:
        if intent == 'move_stage':
            log, notified = change_client_stage(
                profile, args.get('stage'),
                set_by=set_by, note=args.get('note') or '')
            if log is None:
                return {'ok': True, 'message': 'Stage unchanged (idempotent).'}
            return {
                'ok': True,
                'message': (
                    f'Moved to "{args.get("stage")}".'
                    + (' Client emailed.' if notified else
                       ' (Email skipped or failed.)')),
                'extra': {'log_id': str(log.id), 'notified': notified},
            }

        if intent == 'mark_intake_complete':
            intake = mark_intake_complete(profile)
            return {
                'ok': True,
                'message': f'Intake marked complete for {profile.name}.',
                'extra': {'intake_id': str(intake.id)},
            }

        if intent == 'approve_staging':
            log, notified = approve_staging(profile, set_by=set_by)
            return {
                'ok': True,
                'message': f'Moved to pre_launch.',
                'extra': {'log_id': str(log.id) if log else None,
                          'notified': notified},
            }

        if intent == 'mark_live':
            log, notified = mark_live(profile, set_by=set_by)
            return {
                'ok': True,
                'message': f'Launched {profile.name}.',
                'extra': {'log_id': str(log.id) if log else None,
                          'notified': notified},
            }

        if intent == 'add_revision':
            revision, mini = add_revision(
                profile,
                args.get('description') or '',
                is_major=args.get('is_major', True),
                source='ai_assistant')
            msg = f'Revision logged for {profile.name}.'
            if mini is not None:
                msg += (' Out-of-scope: a MiniInvoice was created '
                        '(set the amount + send via the admin action).')
            return {
                'ok': True,
                'message': msg,
                'extra': {
                    'revision_id': str(revision.id),
                    'mini_invoice_id': str(mini.id) if mini else None,
                },
            }

        if intent == 'create_out_of_scope_invoice':
            mini = create_out_of_scope_invoice(
                profile,
                args.get('description') or '',
                args.get('amount'),
                hours=args.get('hours'))
            return {
                'ok': True,
                'message': (
                    f'MiniInvoice created for ${mini.amount} '
                    f'— remember to send it via the admin action.'),
                'extra': {'mini_invoice_id': str(mini.id)},
            }

        if intent == 'get_status':
            return {
                'ok': True,
                'message': f'{profile.name} status:',
                'extra': get_client_status(profile),
            }

        return {
            'ok': False,
            'message': f'Unknown intent "{intent}".',
        }
    except (ValueError, GuardError) as exc:
        return {'ok': False, 'message': str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'message': f'Execution failed: {exc}'}
