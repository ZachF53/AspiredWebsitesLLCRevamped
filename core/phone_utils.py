"""
Phone-number normalisation helper.

We render every phone input on the site with the same `(###) ###-####`
mask (handled client-side by `core/static/js/input_masks.js`). This
module is the server-side canonical form — it accepts whatever the
user actually typed (digits-only, ten-digit-with-dashes, an
international "+1" prefix, etc.) and returns the same `(###) ###-####`
string we store + display.

Usage:
    from core.phone_utils import normalize_phone
    profile.phone = normalize_phone(form.cleaned_data['phone'])
"""

import re

_DIGITS_RE = re.compile(r'\D+')


def normalize_phone(raw):
    """
    Return `raw` formatted as `(###) ###-####`.

    Rules:
      - Returns '' if the input has fewer than 10 digits (we don't
        guess area codes; better to store blank than wrong).
      - Strips an explicit '1' country code prefix when the input has
        11 digits and starts with 1.
      - Anything beyond 10 digits is silently truncated (matches the
        client mask's `maxlength=14` cap).
    """
    if not raw:
        return ''
    digits = _DIGITS_RE.sub('', str(raw))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) < 10:
        # Not a complete US number — return the original so the user
        # can fix it on resubmit. (Truncating to <10 silently would
        # store a half-typed value that looks formatted but isn't.)
        return raw.strip() if isinstance(raw, str) else str(raw)
    digits = digits[:10]
    return f'({digits[:3]}) {digits[3:6]}-{digits[6:]}'
