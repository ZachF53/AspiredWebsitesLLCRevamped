"""
Central trust-level gating for outbound mail.

Every code path that generates an outbound email (cold sender, reply
auto-drafter, manual one-off) MUST call ``should_queue_for_approval``
before deciding whether the message goes into ``pending_approval`` or
straight to ``approved``. Centralising keeps the dial on
``OutreachSettings.trust_level`` honest — flip it in the admin UI and
the very next task tick respects the new policy.

Trust levels (numbered to match ``OutreachSettings.TRUST_LEVEL_CHOICES``):

    1 — MANUAL       every cold and every reply needs approval
    2 — ASSISTED     cold auto-sends; every reply needs approval
    3 — SEMI-AUTO    cold auto-sends; "simple" reply classifications
                     auto-send, complex ones queue
    4 — MOSTLY AUTO  everything auto-sends EXCEPT replies the
                     classifier marked ``needs_human=True``
    5 — AUTONOMOUS   everything auto-sends

Classifications considered "simple" (safe at L3 without human eyes):
just acknowledgements that close the loop and don't require nuance —
unsubscribes (we just suppress, no email back) and clear "not
interested" / "wrong person" rejections. Anything that hints at
ongoing conversation (interested, question, maybe_later, hostile,
unclear) goes to the queue until L4+.
"""

# Reply classifications safe to auto-send a templated answer to at L3.
_SIMPLE_REPLY_CLASSIFICATIONS = frozenset({
    'unsubscribe',
    'not_interested',
    'wrong_person',
})

# Reply classifications the classifier itself flags for human eyes.
_ALWAYS_HUMAN_CLASSIFICATIONS = frozenset({
    'hostile',
    'unclear',
    'question',
})


def should_queue_for_approval(kind, classification=None, needs_human=False):
    """
    Decide whether a generated email goes into the approval queue or
    is auto-promoted to ``approved`` (ready for the send drainer).

    Args:
        kind: 'cold' or 'reply'.
        classification: for replies, the EmailReply.classification
            value (one of EmailReply.CLASSIFICATION_CHOICES). Ignored
            for cold emails.
        needs_human: True when the classifier itself flagged the
            inbound reply as needing a human (overrides classification).

    Returns:
        True to queue (status='pending_approval'),
        False to auto-approve (status='approved').
    """
    from outreach.models import OutreachSettings  # avoid app-loading cycle

    level = OutreachSettings.load().trust_level

    # Level 1: everything waits. Simplest possible policy.
    if level <= 1:
        return True

    if kind == 'cold':
        # L2+ auto-sends cold mail. Warming-cap + daily-cap enforcement
        # happens upstream in the sender — gating only decides approval.
        return False

    # kind == 'reply' from here on.
    # L2: cold auto-sends, replies always queue.
    if level == 2:
        return True

    # L3: simple replies auto-send, complex queue.
    if level == 3:
        if needs_human or classification in _ALWAYS_HUMAN_CLASSIFICATIONS:
            return True
        return classification not in _SIMPLE_REPLY_CLASSIFICATIONS

    # L4: auto unless the classifier flagged it.
    if level == 4:
        return bool(needs_human)

    # L5+: full auto.
    return False


def explain(kind, classification=None, needs_human=False):
    """
    Human-readable reason string for the admin UI — shown next to each
    pending email so the operator knows which policy held it back.
    """
    from outreach.models import OutreachSettings

    level = OutreachSettings.load().trust_level

    if level <= 1:
        return 'Trust level 1 — every email needs approval.'
    if kind == 'cold':
        return ''  # Cold auto-sends at L2+; we wouldn't be here.

    if needs_human:
        return f'Classifier flagged this reply for human review (L{level}).'
    if level == 2:
        return 'Trust level 2 — replies always need approval.'
    if level == 3 and classification in _ALWAYS_HUMAN_CLASSIFICATIONS:
        return f'Reply classified "{classification}" — needs human at L3.'
    if level == 3:
        return f'Reply classified "{classification}" — not in simple set at L3.'
    return f'Held by trust level {level}.'
