/*
 * Aspired Websites — conversion element tracker.
 *
 * Loaded on client sites via:
 *   <script src="https://aspiredwebsites.com/static/js/aspired-tracker.js"
 *           data-aspired-client="CLIENT-UUID" defer></script>
 *
 * Tracks form submissions, phone-number clicks, and CTA-button clicks, and
 * reports them to the Aspired tracking endpoint. No cookies, no PII.
 */
(function () {
    'use strict';

    var ENDPOINT = 'https://aspiredwebsites.com/api/track/';

    // The client UUID comes from this script tag's data attribute. With
    // `defer`, document.currentScript is null at run time, so fall back to
    // a query for the tag.
    var tag = document.currentScript ||
        document.querySelector('script[data-aspired-client]');
    var CLIENT_ID = tag ? tag.getAttribute('data-aspired-client') : '';
    if (!CLIENT_ID) { return; }

    function track(eventType, element) {
        var data = {
            client_id: CLIENT_ID,
            event_type: eventType,
            element_id: (element && element.id) || '',
            element_text: ((element && element.innerText) || '').slice(0, 100),
            page_url: window.location.href,
            page_title: document.title,
            timestamp: new Date().toISOString()
        };
        var body = JSON.stringify(data);
        // sendBeacon survives page unload; fetch+keepalive is the fallback.
        if (navigator.sendBeacon) {
            navigator.sendBeacon(ENDPOINT, body);
        } else {
            fetch(ENDPOINT, { method: 'POST', body: body, keepalive: true });
        }
    }

    // Form submissions.
    document.addEventListener('submit', function (e) {
        if (e.target) { track('form_submit', e.target); }
    });

    // Phone-number (tel:) link clicks.
    document.addEventListener('click', function (e) {
        var link = e.target.closest ? e.target.closest('a') : null;
        if (link && link.href && link.href.indexOf('tel:') === 0) {
            track('phone_click', link);
        }
    });

    // CTA button / link clicks — matched by common call-to-action wording.
    var CTA_PATTERNS = [
        'contact', 'call', 'schedule', 'book', 'consultation',
        'free', 'get started', 'learn more', 'request', 'quote'
    ];
    document.addEventListener('click', function (e) {
        var btn = e.target.closest
            ? e.target.closest('button, a, [role="button"]') : null;
        if (!btn) { return; }
        var text = (btn.innerText || '').toLowerCase();
        for (var i = 0; i < CTA_PATTERNS.length; i++) {
            if (text.indexOf(CTA_PATTERNS[i]) !== -1) {
                track('cta_click', btn);
                return;
            }
        }
    });
})();
