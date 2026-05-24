/*
 * Post-payment success page — 8-second auto-redirect to the
 * account-setup link.
 *
 * The setup URL is rendered server-side via json_script (#setup-url).
 * If it's missing or unreadable, the redirect just doesn't fire and
 * the static fallback CTA in the template still works.
 *
 * Strict-CSP-safe: no inline handlers.
 */
(function () {
    'use strict';

    var raw = document.getElementById('setup-url');
    if (!raw) { return; }

    var url;
    try {
        url = JSON.parse(raw.textContent);
    } catch (e) { return; }

    if (!url || typeof url !== 'string') { return; }

    var countdown = 8;
    var counter = document.getElementById('redirect-countdown');
    var timer = setInterval(function () {
        countdown -= 1;
        if (counter) { counter.textContent = String(countdown); }
        if (countdown <= 0) {
            clearInterval(timer);
            window.location.href = url;
        }
    }, 1000);
})();
