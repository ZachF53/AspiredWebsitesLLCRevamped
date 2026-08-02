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

    # Is this the one public production host?
    #
    # Deliberately NOT derived from SITE_BASE_URL. Staging sets
    # SITE_BASE_URL to its own domain (correctly — staging must not
    # canonicalise to production), so deriving from it would make
    # staging "canonical" for itself and defeat both checks this
    # drives: sitewide noindex on non-production hosts, and the
    # dogfooded tracker. See settings.PRODUCTION_HOST.
    production_host = getattr(
        settings, 'PRODUCTION_HOST', 'aspiredwebsites.com').lower()
    try:
        # Strip any :port before comparing — runserver and the health
        # check both hit the app with one.
        host = request.get_host().split(':', 1)[0].lower()
    except Exception:
        host = ''
    is_production = host == production_host

    return {
        'SITE_BASE_URL': base,
        'CANONICAL_URL': f'{base}{path}',
        'IS_PRODUCTION_HOST': is_production,
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


def analytics(request):
    """
    Expose ``GOOGLE_ANALYTICS_ID`` so `base.html` can load gtag.js.

    Defaults to '' (see settings), and the template renders nothing when
    it is blank — which is what keeps GA off staging and local dev, since
    only prod's `.env` sets the value.

    Unlike the verification tokens this DOES load third-party script and
    send beacons, so `core.middleware` allows the Google hosts in the
    public and payment CSPs.
    """
    return {
        'GOOGLE_ANALYTICS_ID': getattr(
            settings, 'GOOGLE_ANALYTICS_ID', ''),
    }


def analytics_events(request):
    """
    Drain the server-queued conversion events onto this render.

    Pops (see core.analytics), so an event is emitted exactly once even
    if the visitor refreshes the thanks page or navigates back to it.

    Skipped for HTMX partial responses. A partial does not extend
    base.html, so it has nowhere to render the payload — draining the
    queue there would throw the conversion away. /audit/results/ is the
    live example: it loads the AI review over HTMX moments after the
    page that carries the audit_request event.
    """
    if request.headers.get('HX-Request'):
        return {'ANALYTICS_EVENTS': []}
    from core.analytics import pop_events
    return {'ANALYTICS_EVENTS': pop_events(request)}


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
