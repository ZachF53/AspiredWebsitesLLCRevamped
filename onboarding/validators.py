"""
Per-question validation for the onboarding wizard.

``save_answer`` had no validation hook: whatever the client typed was
stored verbatim. That was tolerable while the answers were descriptive
("roughly how old is the site?") and became expensive with the Review
System, where two answers have real consequences:

* A mistyped **EIN** fails the A2P brand registration against IRS
  records. That is the single most common rejection cause and it costs a
  2–5 day round trip to discover.
* A wrong **Google review link** means every review request a customer
  ever receives points somewhere useless, and nobody notices until the
  client asks why they have no new reviews.

Generic by design — a dict keyed on ``question_key``, so any future
product gets validation by adding an entry. Each validator takes the
submitted string and returns a list of human-readable problems (empty
list = fine). None of them raise; a validator that blows up must not
cost the client their answer, so the caller treats an exception as
"valid".
"""

import re

_EIN_RE = re.compile(r'^\d{2}-?\d{7}$')
_ZIP_RE = re.compile(r'^\d{5}(-\d{4})?$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[a-z]{2,}$', re.IGNORECASE)

# Hosts Google actually serves review links from. `g.page` short links
# and the `search.google.com` writereview form are the two shapes the
# "Ask for reviews" button produces; maps.app.goo.gl shows up when a
# client copies from the mobile app.
_GOOGLE_REVIEW_HOSTS = (
    'g.page',
    'search.google.com',
    'maps.google.com',
    'www.google.com',
    'google.com',
    'maps.app.goo.gl',
    'goo.gl',
)


def validate_ein(value):
    digits = re.sub(r'\D', '', value or '')
    if not digits:
        return []  # skippable — a sole proprietor has none
    if not _EIN_RE.match((value or '').strip()):
        return ['An EIN is 9 digits, like 12-3456789.']
    if len(set(digits)) == 1:
        return ['That EIN does not look real — all the digits are the same.']
    # The IRS does not issue prefixes 00, 07, 08, 09, 17, 18, 19, 28, 29,
    # 49, 69, 70, 78, 79 or 89. Checking the obvious one catches a
    # placeholder without pretending to be authoritative.
    if digits.startswith('00'):
        return ['An EIN cannot start with 00.']
    return []


def validate_google_review_url(value):
    url = (value or '').strip()
    if not url:
        return ['Paste your Google review link so requests have somewhere '
                'to send people.']
    if not url.lower().startswith(('http://', 'https://')):
        return ['That does not look like a link — it should start with '
                'https://']
    host = url.split('//', 1)[-1].split('/', 1)[0].split('?', 1)[0].lower()
    host = host.split('@')[-1].split(':')[0]
    if not any(host == h or host.endswith('.' + h)
               for h in _GOOGLE_REVIEW_HOSTS):
        return ['That is not a Google link. In your Google Business '
                'Profile, click "Ask for reviews" and copy the link it '
                'gives you.']
    return []


def validate_http_url(value):
    url = (value or '').strip()
    if not url:
        return []
    if not url.lower().startswith(('http://', 'https://')):
        return ['Start the address with https://']
    if '.' not in url.split('//', 1)[-1].split('/', 1)[0]:
        return ['That does not look like a web address.']
    return []


def validate_zip(value):
    text = (value or '').strip()
    if not text:
        return []
    if not _ZIP_RE.match(text):
        return ['A ZIP code is 5 digits, like 78205.']
    return []


def validate_email(value):
    text = (value or '').strip()
    if not text:
        return []
    if not _EMAIL_RE.match(text):
        return ['That does not look like an email address.']
    return []


def _sms_template(value):
    from reviews.templating import describe_template_problems
    return describe_template_problems(value, channel='sms')


def _email_template(value):
    from reviews.templating import describe_template_problems
    return describe_template_problems(value, channel='email', subject='x')


VALIDATORS = {
    'ein': validate_ein,
    'google_review_url': validate_google_review_url,
    'business_website_url': validate_http_url,
    'business_zip': validate_zip,
    'support_email': validate_email,
    'sms_body_template': _sms_template,
    'email_body_template': _email_template,
}


def describe_answer_problems(question_key, value):
    """Problems with this answer, or []. Never raises.

    An unknown key validates clean — this is opt-in, not a schema.
    """
    validator = VALIDATORS.get(question_key)
    if validator is None:
        return []
    try:
        return list(validator(value) or [])
    except Exception:
        # A broken validator must never cost the client their answer.
        return []
