"""
Reply classification + level-gated auto-reply drafting.

Pipeline for one inbound EmailReply (called from
``classify_and_draft_reply_task``):

    1. Ask Claude (Haiku — fast + cheap) to pick a classification from
       EmailReply.CLASSIFICATION_CHOICES and decide if a human needs to
       see it (sets ``needs_human``).
    2. Honour ``unsubscribe`` immediately — write a SuppressionList row
       and never reply.
    3. For everything else, draft a reply (Sonnet — better at tone)
       and write an EmailSent row in either ``pending_approval`` or
       ``approved`` based on ``outreach.gating.should_queue_for_approval``.

The trust-level dial decides ONE thing here: whether the draft we just
wrote is queued or auto-promoted to ``approved``. The classifier and
drafter always run — even at L1, the operator gets an AI-drafted reply
in their queue so they can edit and approve in seconds.
"""

import json
import logging

from django.utils import timezone

from outreach.gating import should_queue_for_approval
from outreach.models import EmailReply, EmailSent, SuppressionList

logger = logging.getLogger(__name__)


# Classifications safe to auto-reply to with a templated answer at L3+.
# Currently we draft for ALL classifications and let gating decide
# whether the draft goes to approval — but unsubscribes never get a
# reply (we just suppress and move on).
_NO_REPLY_NEEDED = frozenset({'unsubscribe'})


def classify_and_draft(reply):
    """
    Run the full pipeline for one EmailReply. Mutates the reply row
    in-place; returns a dict for the task log:

        {
            'classification': str,
            'needs_human':    bool,
            'drafted':        bool,
            'status':         'sent' | 'approved' | 'pending_approval' | 'skipped',
        }
    """
    out = {
        'classification': '', 'needs_human': False,
        'drafted': False, 'status': 'skipped',
    }

    classification, needs_human = _classify(reply)
    reply.classification = classification
    reply.needs_human = needs_human
    reply.save(update_fields=['classification', 'needs_human'])
    out['classification'] = classification
    out['needs_human'] = needs_human

    # Always honour unsubscribes — write to the suppression list and
    # mark the reply handled. No outbound reply.
    if classification == 'unsubscribe':
        _suppress(reply)
        reply.handled = True
        reply.handled_at = timezone.now()
        reply.save(update_fields=['handled', 'handled_at'])
        out['status'] = 'sent'  # nothing more to do
        return out

    if classification in _NO_REPLY_NEEDED:
        return out

    # Draft a reply — even at L1, so the operator just has to skim+approve.
    try:
        draft_subject, draft_body = _draft_reply(reply)
    except Exception:  # noqa: BLE001
        logger.exception(
            'reply classifier: draft failed for reply %s', reply.pk)
        # Mark needs_human so it lands in Needs You for manual reply.
        reply.needs_human = True
        reply.save(update_fields=['needs_human'])
        out['status'] = 'pending_approval'
        return out

    queue = should_queue_for_approval(
        'reply', classification=classification, needs_human=needs_human)
    status = 'pending_approval' if queue else 'approved'

    from outreach.dispatcher import _from_address as _from  # local import
    EmailSent.objects.create(
        lead=reply.lead,
        in_reply_to=reply,
        kind='reply',
        status=status,
        subject=draft_subject,
        body=draft_body,
        from_email=_from(),
        sequence_step=0,  # replies don't advance the sequence
        approved_at=None if queue else timezone.now(),
    )
    out['drafted'] = True
    out['status'] = status

    # If the reply was auto-handled (no human review needed), mark it.
    if not queue:
        reply.handled = True
        reply.handled_at = timezone.now()
        reply.save(update_fields=['handled', 'handled_at'])

    return out


def _suppress(reply):
    """Add the lead's address to SuppressionList + mirror to Lead."""
    if not reply.lead.email:
        return
    SuppressionList.objects.update_or_create(
        email=reply.lead.email.lower(),
        defaults={
            'domain': reply.lead.email.split('@')[-1].lower(),
            'reason': 'Inbound unsubscribe reply',
        },
    )
    reply.lead.unsubscribed = True
    reply.lead.unsubscribed_at = timezone.now()
    reply.lead.sequence_paused = True
    reply.lead.save(update_fields=[
        'unsubscribed', 'unsubscribed_at', 'sequence_paused', 'updated_at'])


def _classify(reply):
    """
    Ask Claude Haiku to pick a CLASSIFICATION_CHOICES value + decide
    if a human needs to see it. Returns (classification, needs_human).

    Conservative defaults on parse failure: ('unclear', True) so the
    reply lands in the operator's queue rather than getting auto-handled
    on bad AI output.
    """
    from reporting.ai import MODEL_CHAT, claude_complete

    valid = [c for c, _ in EmailReply.CLASSIFICATION_CHOICES]
    system = (
        "You classify inbound replies to cold outreach emails for a "
        "B2B web-design agency. Return ONLY a JSON object — nothing "
        "else, no preamble, no markdown fences — with two fields:\n"
        "  classification: one of " + ', '.join(valid) + "\n"
        "  needs_human: boolean — true if the reply is ambiguous, "
        "hostile, asks a complex question, or otherwise benefits from "
        "human eyes."
    )
    user = (
        f"Inbound reply:\nFrom: {reply.lead.firm_name} "
        f"<{reply.lead.email}>\nSubject: {reply.subject}\n\n"
        f"{reply.body[:4000]}"
    )
    raw = claude_complete(
        messages=[{'role': 'user', 'content': user}],
        system=system, model=MODEL_CHAT, max_tokens=120,
    )
    try:
        # Tolerate models that wrap in ```json fences anyway.
        cleaned = raw.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('\n', 1)[1].rsplit('```', 1)[0]
        parsed = json.loads(cleaned)
        classification = parsed.get('classification', '').strip()
        if classification not in valid:
            classification = 'unclear'
        needs_human = bool(parsed.get('needs_human', True))
        # Some classifications always force human review regardless of
        # the model's opinion — gating layer treats them as such.
        if classification in {'hostile', 'unclear', 'question'}:
            needs_human = True
        return classification, needs_human
    except Exception:  # noqa: BLE001
        logger.exception(
            'classifier: failed to parse Claude output: %r', raw)
        return 'unclear', True


def _draft_reply(reply):
    """
    Use Claude Sonnet to draft an outbound reply. Returns
    (subject, body). Subject is "Re: <original subject>" stripped of
    repeat Re: prefixes; body is plain text, signed by Zachery.
    """
    from reporting.ai import MODEL_CONTENT, claude_complete

    system = (
        "You are Zachery Long, founder of Aspired Websites LLC. Reply "
        "to inbound prospect replies in plain text. Match their tone, "
        "be direct, never salesy. Keep it short — usually 3-6 sentences. "
        "Sign off as '— Zachery'.\n\n"
        "If they asked a question, answer it directly. If they're "
        "interested, propose a 15-minute call (offer Calendly: "
        "https://calendly.com/aspiredwebsites/intro). If they said no "
        "thanks politely, send a brief warm thank-you, leave door open. "
        "If unclear, ask one clarifying question. NEVER include subject "
        "in the body — return only the message body."
    )
    user = (
        f"Original cold-outreach context (what we sent them):\n"
        f"{reply.email_sent.body if reply.email_sent else '(not threaded)'}"
        f"\n\nTheir reply:\n{reply.body[:3000]}"
    )
    body = claude_complete(
        messages=[{'role': 'user', 'content': user}],
        system=system, model=MODEL_CONTENT, max_tokens=500,
    ).strip()

    subject = (reply.subject or '').strip()
    # Strip duplicate Re: prefixes — keep at most one.
    while subject.lower().startswith('re:'):
        subject = subject[3:].strip()
    subject = f'Re: {subject}' if subject else 'Re: (no subject)'
    return subject[:255], body
