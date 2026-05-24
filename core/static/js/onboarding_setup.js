/*
 * Onboarding setup page — client-side validation hints + PIN UX.
 *
 * Two independent jobs:
 *  1. Live password-requirement hints (length / has digit / passwords match).
 *     Hint elements carry `data-hint="length|number|match"` and toggle the
 *     `is-valid` class as the user types — CSS handles the colour + glyph.
 *
 *  2. PIN box auto-advance. Each PIN group has 4 single-digit <input> boxes
 *     wrapped in a `.setup-pin-row[data-pin-group]`. Typing advances focus
 *     forward, backspace on an empty box jumps back, paste of a 4-digit
 *     string spreads across the four boxes.
 *
 * Strict-CSP-safe: no inline handlers, no eval. Defer-loaded by
 * onboarding_setup.html so the DOM is parsed by the time we run.
 */
(function () {
    'use strict';

    // ── 1. Password hints ────────────────────────────────────────────
    var p1 = document.getElementById('setup-password');
    var p2 = document.getElementById('setup-password-confirm');
    var hintLen = document.querySelector('[data-hint="length"]');
    var hintNum = document.querySelector('[data-hint="number"]');
    var hintMatch = document.querySelector('[data-hint="match"]');

    function syncPasswordHints() {
        if (!p1) { return; }
        var v = p1.value || '';
        if (hintLen) { hintLen.classList.toggle('is-valid', v.length >= 8); }
        if (hintNum) { hintNum.classList.toggle('is-valid', /\d/.test(v)); }
        if (hintMatch) {
            var match = p2 && p2.value.length > 0 && p2.value === v;
            hintMatch.classList.toggle('is-valid', !!match);
        }
    }
    if (p1) { p1.addEventListener('input', syncPasswordHints); }
    if (p2) { p2.addEventListener('input', syncPasswordHints); }

    // ── 2. PIN auto-advance ──────────────────────────────────────────
    function wirePinGroup(group) {
        var boxes = group.querySelectorAll('input');
        boxes.forEach(function (box, idx) {
            box.addEventListener('input', function () {
                box.value = (box.value || '').replace(/\D/g, '').slice(0, 1);
                if (box.value && idx < boxes.length - 1) {
                    boxes[idx + 1].focus();
                }
            });
            box.addEventListener('keydown', function (e) {
                if (e.key === 'Backspace' && !box.value && idx > 0) {
                    boxes[idx - 1].focus();
                }
            });
            box.addEventListener('paste', function (e) {
                var data = (e.clipboardData || window.clipboardData)
                    .getData('text');
                var digits = (data || '').replace(/\D/g, '').slice(0, 4);
                if (digits.length === 4) {
                    e.preventDefault();
                    boxes.forEach(function (b, i) {
                        b.value = digits[i] || '';
                    });
                    boxes[3].focus();
                }
            });
        });
    }
    document.querySelectorAll('[data-pin-group]').forEach(wirePinGroup);
})();
