/*
 * Post-payment success page — auto-redirect to wherever the buyer
 * should go next.
 *
 * Two cases: a first payment sends them to account setup, a later one
 * (the final balance, say) sends them to their portal — they already
 * have an account. The target is rendered server-side via json_script
 * (#redirect-url), and the delay comes from data-seconds on the
 * countdown element, so the template decides both.
 *
 * #setup-url is still read as a fallback for any cached page rendered
 * before this was generalised.
 *
 * If the target is missing or unreadable the redirect just doesn't
 * fire, and the static CTA in the template still works.
 *
 * Strict-CSP-safe: no inline handlers.
 */
(function () {
    'use strict';

    var raw = document.getElementById('redirect-url')
        || document.getElementById('setup-url');
    if (!raw) { return; }

    var url;
    try {
        url = JSON.parse(raw.textContent);
    } catch (e) { return; }

    if (!url || typeof url !== 'string') { return; }

    var counter = document.getElementById('redirect-countdown');
    var seconds = parseInt(
        (counter && counter.getAttribute('data-seconds')) || '8', 10);
    if (!(seconds > 0)) { seconds = 8; }

    var countdown = seconds;
    var timer = setInterval(function () {
        countdown -= 1;
        if (counter) { counter.textContent = String(countdown); }
        if (countdown <= 0) {
            clearInterval(timer);
            window.location.href = url;
        }
    }, 1000);
})();
