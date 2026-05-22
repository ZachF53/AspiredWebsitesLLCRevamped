/* Portal credentials vault — PIN entry, countdowns, password reveal/copy,
   and HTMX session re-auth.
   CSP-safe: external file, no inline handlers, no eval. */
(function () {
    'use strict';

    // ── Password show/hide toggles (PIN setup page) ──────────────────────
    document.querySelectorAll('.vault-pw-toggle').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var input = document.getElementById(btn.getAttribute('data-target'));
            if (!input) { return; }
            if (input.type === 'password') {
                input.type = 'text';
                btn.textContent = 'Hide';
            } else {
                input.type = 'password';
                btn.textContent = 'Show';
            }
        });
    });

    // ── PIN digit boxes — auto-advance + auto-submit ─────────────────────
    // requestSubmit() (not submit()) dispatches a real submit event, so an
    // HTMX-bound form is intercepted correctly while a plain form posts.
    function wireDigits(boxes) {
        if (!boxes.length) { return; }
        boxes.forEach(function (box, i) {
            box.addEventListener('input', function () {
                box.value = box.value.replace(/[^0-9]/g, '').slice(0, 1);
                if (box.value && i < boxes.length - 1) {
                    boxes[i + 1].focus();
                }
                var filled = boxes.every(function (d) {
                    return d.value.length === 1;
                });
                if (filled) {
                    var form = box.closest('form');
                    if (form) { form.requestSubmit(); }
                }
            });
            box.addEventListener('keydown', function (e) {
                if (e.key === 'Backspace' && !box.value && i > 0) {
                    boxes[i - 1].focus();
                }
            });
        });
    }

    var mainDigits = Array.prototype.slice.call(
        document.querySelectorAll('#pvault-pin-form .vault-digit'));
    wireDigits(mainDigits);

    var reauthDigits = Array.prototype.slice.call(
        document.querySelectorAll('.pvault-reauth-digit'));
    wireDigits(reauthDigits);

    function clearReauthDigits() {
        reauthDigits.forEach(function (d) { d.value = ''; });
        if (reauthDigits.length) { reauthDigits[0].focus(); }
    }

    // ── Lockout countdown ────────────────────────────────────────────────
    var lockEl = document.getElementById('pvault-lockout-countdown');
    if (lockEl) {
        var until = new Date(lockEl.getAttribute('data-until')).getTime();
        var tickLock = function () {
            var left = Math.floor((until - Date.now()) / 1000);
            if (left <= 0) {
                window.location.reload();
                return;
            }
            var m = Math.floor(left / 60), s = left % 60;
            lockEl.textContent = m + ':' + (s < 10 ? '0' : '') + s;
        };
        tickLock();
        setInterval(tickLock, 1000);
    }

    // ── Session countdown + expiry overlay ───────────────────────────────
    var wrap = document.querySelector('.pvault[data-seconds-remaining]');
    var countEl = document.getElementById('pvault-countdown');
    var overlay = document.getElementById('pvault-overlay');
    if (wrap && countEl) {
        var remaining = parseInt(wrap.getAttribute('data-seconds-remaining'), 10) || 0;
        var showOverlay = function () {
            if (overlay) {
                overlay.hidden = false;
                if (reauthDigits.length) { reauthDigits[0].focus(); }
            }
        };
        var tick = function () {
            if (remaining <= 0) {
                countEl.textContent = '0:00';
                var badge = document.getElementById('pvault-session');
                if (badge) { badge.classList.add('is-expired'); }
                showOverlay();
                return;
            }
            var m = Math.floor(remaining / 60), s = remaining % 60;
            countEl.textContent = m + ':' + (s < 10 ? '0' : '') + s;
            remaining -= 1;
            setTimeout(tick, 1000);
        };
        tick();
    }

    // ── Password reveal toggles ──────────────────────────────────────────
    document.querySelectorAll('.pcred__reveal').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var row = btn.closest('.pcred__row');
            if (!row) { return; }
            var dots = row.querySelector('.pcred__dots');
            var plain = row.querySelector('.pcred__plain');
            if (!dots || !plain) { return; }
            var willMask = !plain.hidden;  // currently revealed → mask it
            plain.hidden = willMask;
            dots.hidden = !willMask;
            btn.textContent = willMask ? 'Show' : 'Hide';
            btn.setAttribute('aria-pressed', willMask ? 'false' : 'true');
        });
    });

    // ── Copy buttons ─────────────────────────────────────────────────────
    document.querySelectorAll('.pcred__copy').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var row = btn.closest('.pcred__row');
            if (!row) { return; }
            var plain = row.querySelector('.pcred__plain');
            if (!plain || !navigator.clipboard) { return; }
            navigator.clipboard.writeText(plain.textContent).then(function () {
                var original = btn.textContent;
                btn.textContent = 'Copied';
                btn.classList.add('is-copied');
                setTimeout(function () {
                    btn.textContent = original;
                    btn.classList.remove('is-copied');
                }, 1500);
            });
        });
    });

    // ── HTMX re-auth result ──────────────────────────────────────────────
    // Success fires HX-Trigger 'vaultReauthed' → reload for a fresh window.
    document.body.addEventListener('vaultReauthed', function () {
        window.location.reload();
    });
    // A wrong PIN swaps an error into the result box — clear the digits.
    document.body.addEventListener('htmx:afterSwap', function (e) {
        if (e.target && e.target.id === 'pvault-reauth-result') {
            clearReauthDigits();
        }
    });
})();
