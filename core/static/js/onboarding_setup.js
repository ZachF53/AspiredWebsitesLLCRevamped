/*
 * PIN auto-advance for the onboarding setup page.
 *
 * Each PIN group has 4 single-digit <input> boxes (data-pin-group="pin"
 * or "pin_confirm"). Typing in box N moves focus to box N+1; backspace
 * on an empty box goes to box N-1; digits-only.
 */
(function () {
    'use strict';

    function wire(group) {
        var boxes = group.querySelectorAll('input');
        boxes.forEach(function (box, idx) {
            box.addEventListener('input', function (e) {
                // Strip any non-digit the user pasted/typed.
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
            // Paste a 4-digit PIN into the first box → spread across boxes.
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

    document.querySelectorAll('[data-pin-group]').forEach(wire);
})();
