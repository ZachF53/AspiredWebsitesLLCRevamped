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

    // Global Privacy Control: GA4 sends visitor data to Google, so a
    // browser that asks not to have its data shared gets no GA at all.
    // events.js calls window.gtag only if it exists, so this is a clean
    // no-op for the conversion events too.
    if (navigator.globalPrivacyControl === true) { return; }

    // gtag.js reads window.dataLayer, so the queue must exist before the
    // remote script lands. Ordering is deliberate: define the shim first,
    // then request the library.
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    window.gtag = gtag;

    gtag('js', new Date());

    // allow_google_signals: false — Google Signals adds a second beacon
    // to https://www.google.com/g/collect (cross-device + demographics,
    // and the data Google uses for ads personalisation). Our connect-src
    // allows *.google-analytics.com and *.analytics.google.com but NOT
    // google.com, so that request was already being refused by the CSP:
    // it never delivered anything, it just logged a CSP violation to the
    // console on every single page view.
    //
    // Turning it off at the source makes it fail cleanly instead of
    // loudly. Core measurement is untouched — pageviews and every §5
    // conversion go to www.google-analytics.com/g/collect, which is
    // allowed and verified working.
    //
    // The alternative was widening connect-src to include google.com.
    // Not taken: it buys demographic reporting at the cost of a broader
    // policy and of sending visitor data to an ads endpoint, which sits
    // badly with the security-first positioning. To reverse it, delete
    // this flag AND add https://www.google.com to connect-src in
    // core/middleware.py — one without the other just restores the
    // blocked request and the console noise.
    gtag('config', id, { allow_google_signals: false });

    // The library itself is fetched only after the page has finished
    // loading. gtag.js is ~850 ms of main-thread work on a throttled
    // phone (Lighthouse, Sept 2026 baseline) and was the largest single
    // contributor to mobile LCP. Nothing is lost by waiting: every
    // gtag() call before then sits in dataLayer and is sent when the
    // library arrives.
    function loadLibrary() {
        var lib = document.createElement('script');
        lib.async = true;
        lib.src = 'https://www.googletagmanager.com/gtag/js?id='
            + encodeURIComponent(id);
        document.head.appendChild(lib);
    }
    function whenIdle() {
        if (typeof window.requestIdleCallback === 'function') {
            window.requestIdleCallback(loadLibrary, { timeout: 3000 });
        } else {
            setTimeout(loadLibrary, 1200);
        }
    }
    // main.js (loaded first, also deferred) provides aspiredWhenSettled:
    // first interaction or a few seconds after load. Idle-after-load
    // alone still ran gtag.js before a throttled phone's first paint.
    if (typeof window.aspiredWhenSettled === 'function') {
        window.aspiredWhenSettled(loadLibrary);
    } else if (document.readyState === 'complete') { whenIdle(); }
    else { window.addEventListener('load', whenIdle, { once: true }); }
})();
