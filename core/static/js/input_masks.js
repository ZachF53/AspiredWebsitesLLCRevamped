/*
 * Site-wide input UX:
 *
 *   1. Phone mask. Every `input[type=tel]` on the page (except those
 *      with `data-no-phone-mask` — e.g. the 4-digit vault PIN boxes
 *      which also use type=tel for the numeric keypad on mobile) is
 *      formatted to `(###) ###-####` as the user types. Strips
 *      non-digits, truncates to 10 digits, formats progressively.
 *
 *   2. Autocaps-off enforcement on email + password fields. Set as
 *      HTML attributes too in the templates (canonical), but this
 *      script catches any field the templates miss — mobile browsers
 *      honor the runtime-set attribute as long as it's set before
 *      first focus.
 *
 * Loaded once from every base template; strict-CSP-safe (no inline
 * handlers, no eval).
 */
(function () {
    'use strict';

    // ── 1. Phone mask ────────────────────────────────────────────────
    function formatPhone(value) {
        var digits = (value || '').replace(/\D/g, '').slice(0, 10);
        if (!digits) { return ''; }
        if (digits.length < 4)  { return '(' + digits; }
        if (digits.length < 7)  { return '(' + digits.slice(0, 3) + ') ' + digits.slice(3); }
        return '(' + digits.slice(0, 3) + ') ' +
               digits.slice(3, 6) + '-' +
               digits.slice(6);
    }

    function wirePhone(input) {
        if (input.dataset.noPhoneMask !== undefined) { return; }
        // PIN boxes mark themselves with maxlength=1 — never mask those.
        if (parseInt(input.getAttribute('maxlength') || '0', 10) === 1) {
            return;
        }
        // Mobile numeric keypad without the +/* extras.
        if (!input.getAttribute('inputmode')) {
            input.setAttribute('inputmode', 'tel');
        }
        if (!input.getAttribute('placeholder')) {
            input.setAttribute('placeholder', '(210) 555-1234');
        }
        if (!input.getAttribute('autocomplete')) {
            input.setAttribute('autocomplete', 'tel');
        }
        if (!input.getAttribute('maxlength') ||
                parseInt(input.getAttribute('maxlength'), 10) < 14) {
            input.setAttribute('maxlength', '14');  // "(123) 456-7890"
        }

        // Initial format — server may render an unformatted value
        // (e.g. legacy "2105551234" stored before the mask existed).
        if (input.value) {
            input.value = formatPhone(input.value);
        }

        input.addEventListener('input', function () {
            // Preserve caret-at-end behaviour. Reformatting always
            // sets the cursor to the end of the masked string, which
            // matches what the user expects when they're typing the
            // next digit forward.
            input.value = formatPhone(input.value);
        });
        input.addEventListener('paste', function (e) {
            var data = (e.clipboardData || window.clipboardData)
                .getData('text');
            if (!data) { return; }
            e.preventDefault();
            input.value = formatPhone(data);
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });
    }

    // ── 2. Autocaps off on email + password ──────────────────────────
    function killAutocaps(input) {
        input.setAttribute('autocapitalize', 'none');
        input.setAttribute('autocorrect', 'off');
        input.setAttribute('spellcheck', 'false');
    }

    function init() {
        document.querySelectorAll('input[type="tel"]').forEach(wirePhone);
        document.querySelectorAll(
            'input[type="email"], input[type="password"]'
        ).forEach(killAutocaps);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
