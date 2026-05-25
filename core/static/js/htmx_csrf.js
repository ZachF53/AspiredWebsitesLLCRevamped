/**
 * HTMX ↔ Django CSRF bridge.
 *
 * Every htmx-issued POST/PUT/PATCH/DELETE needs Django's CSRF token
 * in the `X-CSRFToken` header. HTMX doesn't add it automatically, so
 * without this hook every hx-post silently fails with 403 — the
 * button just appears to do nothing because the target div renders
 * the empty error body.
 *
 * Reads the token from the `csrftoken` cookie (Django's default cookie
 * name; can be overridden in settings via CSRF_COOKIE_NAME but we use
 * the default).
 *
 * Loaded by both the admin and the portal base templates because
 * both serve htmx-driven pages.
 */
(function () {
    function getCookie(name) {
        var prefix = name + '=';
        var parts = document.cookie.split(';');
        for (var i = 0; i < parts.length; i++) {
            var c = parts[i].trim();
            if (c.indexOf(prefix) === 0) {
                return decodeURIComponent(c.substring(prefix.length));
            }
        }
        return null;
    }

    document.addEventListener('htmx:configRequest', function (evt) {
        var token = getCookie('csrftoken');
        if (token) {
            evt.detail.headers['X-CSRFToken'] = token;
        }
    });
})();
