"""
Minimal User-Agent parser — device class, browser, and OS.

Deliberately dependency-free. The alternative (`user-agents` /
`ua-parser`) pulls a regex database we would have to keep updated on
every droplet for a field that is only ever shown as a label next to a
session recording. This covers the traffic a small-business site
actually gets; anything unrecognised degrades to 'unknown' rather than
guessing.

Order matters throughout — Edge advertises Chrome, Chrome advertises
Safari, and Opera advertises both, so the most specific token has to be
tested first.

    >>> parse_user_agent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 ...)')
    {'device_type': 'mobile', 'browser': 'Safari', 'os': 'iOS'}
"""

import re

DEVICE_CHOICES = [
    ('desktop', 'Desktop'),
    ('mobile', 'Mobile'),
    ('tablet', 'Tablet'),
    ('bot', 'Bot'),
    ('unknown', 'Unknown'),
]

# Emoji shown next to the label in the admin list.
DEVICE_ICONS = {
    'desktop': '🖥',
    'mobile': '📱',
    'tablet': '▭',
    'bot': '🤖',
    'unknown': '·',
}

_BOT = re.compile(
    r'bot|crawl|spider|slurp|bingpreview|headlesschrome|phantomjs|'
    r'lighthouse|pagespeed|gtmetrix|pingdom|uptimerobot|curl|wget|'
    r'python-requests|axios|monitoring',
    re.I,
)

# (label, compiled pattern) — first match wins.
_BROWSERS = [
    ('Samsung Internet', re.compile(r'SamsungBrowser/([\d.]+)')),
    ('Edge', re.compile(r'Edg(?:e|A|iOS)?/([\d.]+)')),
    ('Opera', re.compile(r'(?:OPR|OPiOS)/([\d.]+)')),
    ('Firefox', re.compile(r'(?:Firefox|FxiOS)/([\d.]+)')),
    ('Chrome', re.compile(r'(?:Chrome|CriOS)/([\d.]+)')),
    ('Safari', re.compile(r'Version/([\d.]+).*Safari')),
    ('IE', re.compile(r'(?:MSIE |Trident.*rv:)([\d.]+)')),
]


def _browser(ua):
    for label, pattern in _BROWSERS:
        m = pattern.search(ua)
        if m:
            major = (m.group(1) or '').split('.')[0]
            return f'{label} {major}' if major else label
    return ''


def _os(ua):
    if re.search(r'iPhone|iPad|iPod', ua):
        m = re.search(r'OS (\d+)[._]', ua)
        return f'iOS {m.group(1)}' if m else 'iOS'
    if 'Android' in ua:
        m = re.search(r'Android (\d+)', ua)
        return f'Android {m.group(1)}' if m else 'Android'
    if 'CrOS' in ua:
        return 'ChromeOS'
    if 'Mac OS X' in ua:
        m = re.search(r'Mac OS X (\d+)[._](\d+)', ua)
        return f'macOS {m.group(1)}.{m.group(2)}' if m else 'macOS'
    if 'Windows NT' in ua:
        # Microsoft froze the NT version at 10.0 for Windows 11, so this
        # cannot distinguish 10 from 11 without client hints. Report the
        # marketing name we can actually justify.
        m = re.search(r'Windows NT ([\d.]+)', ua)
        return {
            '10.0': 'Windows 10/11',
            '6.3': 'Windows 8.1',
            '6.2': 'Windows 8',
            '6.1': 'Windows 7',
        }.get(m.group(1) if m else '', 'Windows')
    if 'Linux' in ua:
        return 'Linux'
    return ''


def _device_type(ua):
    if _BOT.search(ua):
        return 'bot'
    if 'iPad' in ua:
        return 'tablet'
    # iPadOS 13+ masquerades as desktop Safari; the touch-capable
    # Macintosh signature is the only tell left in the UA string.
    if 'Macintosh' in ua and 'Mobile' in ua:
        return 'tablet'
    if 'Android' in ua:
        # Android phones carry "Mobile"; Android tablets omit it.
        return 'mobile' if 'Mobile' in ua else 'tablet'
    if re.search(r'iPhone|iPod|Windows Phone|IEMobile|BlackBerry|Opera Mini',
                 ua):
        return 'mobile'
    if 'Mobile' in ua:
        return 'mobile'
    if re.search(r'Tablet|Kindle|Silk|PlayBook', ua, re.I):
        return 'tablet'
    if re.search(r'Windows|Macintosh|Linux|CrOS|X11', ua):
        return 'desktop'
    return 'unknown'


def parse_user_agent(ua):
    """Return {'device_type', 'browser', 'os'} for a UA string.

    Never raises and never guesses past what the string supports — an
    empty or unrecognised UA yields device_type='unknown' and empty
    browser/os rather than a plausible-looking default.
    """
    ua = (ua or '').strip()
    if not ua:
        return {'device_type': 'unknown', 'browser': '', 'os': ''}
    return {
        'device_type': _device_type(ua),
        'browser': _browser(ua),
        'os': _os(ua),
    }
