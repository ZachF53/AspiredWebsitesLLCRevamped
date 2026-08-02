/*
 * Google Analytics 4 loader for the marketing site.
 *
 * Google's copy-paste snippet is an inline <script>, which our public CSP
 * (script-src 'self') blocks outright. Rather than weaken the policy with
 * 'unsafe-inline' — the single most useful directive we have against XSS —
 * the snippet's body lives here as an external file and the measurement id
 * arrives via a data attribute on the tag:
 *
 *     <script src="/static/js/analytics.js" data-ga-id="G-XXXXXXXXXX" defer>
 *
 * Same pattern as aspired-tracker.js (data-aspired-client). CSP then only
 * has to allow the googletagmanager.com host, not inline execution.
 */
(function () {
    'use strict';

    // document.currentScript is set for classic deferred scripts; the
    // querySelector is a fallback for any context where it is not.
    var tag = document.currentScript
        || document.querySelector('script[data-ga-id]');
    if (!tag) { return; }

    var id = tag.getAttribute('data-ga-id');
    if (!id) { return; }

    // gtag.js reads window.dataLayer, so the queue must exist before the
    // remote script lands. Ordering is deliberate: define the shim first,
    // then request the library.
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    window.gtag = gtag;

    gtag('js', new Date());
    gtag('config', id);

    var lib = document.createElement('script');
    lib.async = true;
    lib.src = 'https://www.googletagmanager.com/gtag/js?id='
        + encodeURIComponent(id);
    document.head.appendChild(lib);
})();
