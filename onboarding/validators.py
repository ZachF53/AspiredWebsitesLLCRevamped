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


def validate_us_phone(value):
    """A number we will actually have to dial.

    The receptionist's transfer number is the one answer where a typo is
    silent and expensive: the agent takes the call, tries to put the
    customer through, and the transfer fails every time. Nobody finds out
    until the client asks why they never get handed a live caller.
    """
    raw = (value or '').strip()
    if not raw:
        return []
    digits = re.sub(r'\D', '', raw)
    if digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return ['That does not look like a 10-digit US phone number, '
                'e.g. (210) 555-0142.']
    if digits[0] in '01':
        return ['US area codes do not start with 0 or 1 — check the '
                'first three digits.']
    return []


# "closed" is a first-class answer, not a missing one: most HVAC shops
# are shut at the weekend and that is exactly when the receptionist
# earns its keep.
_CLOSED_WORDS = {'closed', 'none', 'n/a', 'na', '-', 'shut'}
_TIME_RANGE_RE = re.compile(
    r'^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*'
    r'(?:-|to|–|—)\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$',
    re.IGNORECASE)


def validate_business_hours(value):
    """A day's trading hours, as a range or the word "closed".

    Parsed rather than free-text because the after-hours schedule is
    computed as the inverse of these windows. An unparseable answer means
    we cannot work out when the agent should be live, and the failure
    would show up as the receptionist covering the wrong half of the day.
    """
    raw = (value or '').strip()
    if not raw or raw.lower() in _CLOSED_WORDS:
        return []
    match = _TIME_RANGE_RE.match(raw)
    if not match:
        return ['Write it as a range like "8:00am - 5:00pm", or just '
                '"closed".']
    start_h, _, start_ap, end_h, _, end_ap = match.groups()
    if not start_ap and not end_ap:
        return ['Add am or pm so we know which 8 you mean — "8:00am - '
                '5:00pm".']
    for hour in (int(start_h), int(end_h)):
        if hour == 0 or hour > 12:
            return ['Use 12-hour times with am or pm, like "8:00am - '
                    '5:00pm".']
    return []


VALIDATORS = {
    'ein': validate_ein,
    'google_review_url': validate_google_review_url,
    'business_website_url': validate_http_url,
    'business_zip': validate_zip,
    'support_email': validate_email,
    'sms_body_template': _sms_template,
    'email_body_template': _email_template,
    # AI Voice Receptionist
    'business_phone': validate_us_phone,
    'transfer_number': validate_us_phone,
    'hours_mon_fri': validate_business_hours,
    'hours_saturday': validate_business_hours,
    'hours_sunday': validate_business_hours,
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
