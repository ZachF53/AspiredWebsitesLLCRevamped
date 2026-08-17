"""
Small helpers shared by the split view modules.

Kept apart from context.py, which is specifically the admin chrome
context. These are plain utilities with no request involvement.
"""

import uuid


def _is_uuid(value):
    """True if `value` parses as a UUID — guards filters against bad params."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False
