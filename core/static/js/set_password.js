// set_password.js — live validation for the self-checkout set-password
// screen. Shows a green ✓ / red ✕ per password rule as the user types,
// a "Passwords match" indicator under confirm, and a "PINs match"
// indicator under PIN confirm. Disables the submit button until every
// rule + match passes (the server enforces the same rules regardless,
// so if this script never runs the button stays enabled and the server
// validates). External file — CSP_PUBLIC forbids inline scripts.
(function () {
    'use strict';

    var pw = document.getElementById('id_password');
    var pwConfirm = document.getElementById('id_confirm_password');
    var pin = document.getElementById('id_pin');
    var pinConfirm = document.getElementById('id_pin_confirm');
    var submit = document.getElementById('set-password-submit');
    if (!pw || !pwConfirm || !pin || !pinConfirm) return;

    // Keep these in lock-step with _password_rule_error() in
    // onboarding/password_views.py.
    var rules = {
        length: function (v) { return v.length >= 8; },
        lower: function (v) { return /[a-z]/.test(v); },
        upper: function (v) { return /[A-Z]/.test(v); },
        number: function (v) { return /[0-9]/.test(v); },
        special: function (v) { return /[^A-Za-z0-9]/.test(v); }
    };

    var ruleEls = {};
    Object.keys(rules).forEach(function (k) {
        ruleEls[k] = document.querySelector('[data-rule="' + k + '"]');
    });
    var pwMatchEl = document.getElementById('pw-match');
    var pinMatchEl = document.getElementById('pin-match');

    function evaluate() {
        var pwVal = pw.value;
        var allPw = true;
        Object.keys(rules).forEach(function (k) {
            var ok = rules[k](pwVal);
            if (ruleEls[k]) ruleEls[k].classList.toggle('valid', ok);
            if (!ok) allPw = false;
        });

        var pwMatch = pwConfirm.value.length > 0 && pwConfirm.value === pwVal;
        if (pwMatchEl) {
            pwMatchEl.hidden = pwConfirm.value.length === 0;
            pwMatchEl.classList.toggle('ok', pwMatch);
            pwMatchEl.textContent =
                pwMatch ? 'Passwords match' : 'Passwords do not match yet';
        }

        var pinOk = /^[0-9]{4}$/.test(pin.value);
        var pinMatch = pinOk && pinConfirm.value === pin.value;
        if (pinMatchEl) {
            pinMatchEl.hidden = pinConfirm.value.length === 0;
            pinMatchEl.classList.toggle('ok', pinMatch);
            pinMatchEl.textContent =
                pinMatch ? 'PINs match' : 'PINs do not match yet';
        }

        if (submit) {
            submit.disabled = !(allPw && pwMatch && pinOk && pinMatch);
        }
    }

    [pw, pwConfirm, pin, pinConfirm].forEach(function (el) {
        el.addEventListener('input', evaluate);
    });
    evaluate();
})();
