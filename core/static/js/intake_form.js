/*
 * Intake form — conditional reveals.
 *
 * Each `.intake-reveal` element opts in via `data-reveal-when="<id>"`,
 * pointing at the input that controls its visibility. Two modes:
 *
 *   - data-reveal-when="id_photos_provided"
 *     (no data-reveal-value) → checkbox; reveal when checked.
 *
 *   - data-reveal-when="id_domain_registrar" data-reveal-value="other"
 *     → <select>; reveal when the value matches.
 *
 * Class toggle is `is-revealed` — CSS handles the actual hide/show
 * (display: none by default → block when present). Strict-CSP-safe;
 * no inline handlers.
 */
(function () {
    'use strict';

    function syncReveal(reveal) {
        var controlId = reveal.getAttribute('data-reveal-when');
        var expected = reveal.getAttribute('data-reveal-value');
        var control = document.getElementById(controlId);
        if (!control) { return; }

        var shouldReveal;
        if (expected === null) {
            // No data-reveal-value → treat the control as a checkbox.
            shouldReveal = !!control.checked;
        } else {
            shouldReveal = (control.value === expected);
        }
        reveal.classList.toggle('is-revealed', shouldReveal);
    }

    function wireReveal(reveal) {
        var controlId = reveal.getAttribute('data-reveal-when');
        var control = document.getElementById(controlId);
        if (!control) { return; }
        var ev = (control.tagName === 'SELECT' || control.type === 'checkbox')
            ? 'change' : 'input';
        control.addEventListener(ev, function () { syncReveal(reveal); });
        // Run once to sync initial server-rendered state with the
        // control's actual DOM value (browser autofill / back-button).
        syncReveal(reveal);
    }

    document.querySelectorAll('.intake-reveal').forEach(wireReveal);
})();
