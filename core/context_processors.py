"""
Template context processors registered in `settings.TEMPLATES`.

These run on every template render — keep them cheap.
"""

from django.conf import settings


def static_version(request):
    """
    Exposes `STATIC_VERSION` to all templates so base templates can
    cache-bust their static asset URLs:

        <link rel="stylesheet" href="{% static 'css/main.css' %}?v={{ STATIC_VERSION }}">

    Settings derives the value from the current git short SHA at
    process start, so every deploy yields a new value and every
    browser re-fetches CSS/JS without relying on the static-storage
    manifest (which is currently not generating hashed filenames in
    prod — see `STATIC_VERSION` in settings.py for the why).
    """
    return {'STATIC_VERSION': getattr(settings, 'STATIC_VERSION', '1')}


def canonical(request):
    """
    Expose ``CANONICAL_URL`` and ``SITE_BASE_URL`` for the self-referencing
    canonical link, og:url, and absolute schema URLs.

    Built from ``settings.SITE_BASE_URL`` + ``request.path`` rather than
    ``request.build_absolute_uri()`` on purpose. The site answers on both
    ``www`` and non-``www``; a canonical derived from the request would
    happily declare ``https://www.…`` as canonical when reached that way,
    which is exactly the duplicate-content split the canonical exists to
    resolve. Master Plan §8 requires one host, and `SITE_BASE_URL` is it.

    ``request.path`` also drops the query string, so
    ``/audit/results/?url=…`` canonicalises to ``/audit/results/``.
    """
    base = getattr(settings, 'SITE_BASE_URL',
                   'https://aspiredwebsites.com').rstrip('/')
    path = getattr(request, 'path', '/') or '/'

    # Is this request being served by the canonical production host?
    #
    # Used to gate the dogfooded conversion tracker. That script is
    # built for CLIENT sites on other domains, so it hardcodes
    # absolute https://aspiredwebsites.com API endpoints — correct for
    # its real job. But when we run it on our own staging host those
    # calls become cross-origin, the public CSP (connect-src 'self')
    # blocks them, and every staging page logs console errors while
    # silently trying to report into production's analytics.
    try:
        canonical_host = base.split('//', 1)[-1].split('/', 1)[0].lower()
        is_canonical = request.get_host().lower() == canonical_host
    except Exception:
        is_canonical = False

    return {
        'SITE_BASE_URL': base,
        'CANONICAL_URL': f'{base}{path}',
        'IS_CANONICAL_HOST': is_canonical,
    }


def site_verification(request):
    """
    Expose the Google Search Console + Bing Webmaster ownership tokens
    so `base.html` can render their verification <meta> tags.

    Both default to '' (see settings), and the template renders nothing
    when a token is blank — so this is a no-op until the properties are
    actually created. Token-only meta tags: no third-party request, no
    script, no CSP change.
    """
    return {
        'GOOGLE_SITE_VERIFICATION': getattr(
            settings, 'GOOGLE_SITE_VERIFICATION', ''),
        'BING_SITE_VERIFICATION': getattr(
            settings, 'BING_SITE_VERIFICATION', ''),
    }


def system_alerts(request):
    """
    Expose ``system_alerts_unresolved`` (int count) to every template
    so the admin dashboard banner renders consistently. Cheap query —
    one COUNT() against an indexed (resolved_at, created_at) column.
    Defensive: returns 0 if the SystemAlert table doesn't exist yet.
    """
    try:
        from core.system_alerts import recent_unresolved_count
        return {'system_alerts_unresolved': recent_unresolved_count()}
    except Exception:
        return {'system_alerts_unresolved': 0}
