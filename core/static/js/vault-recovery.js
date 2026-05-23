/* Vault recovery codes — Download / Copy / "I have saved" gating.
   CSP-safe: external file, no inline handlers, no eval. */
(function () {
    'use strict';

    function collectCodes() {
        return Array.prototype.map.call(
            document.querySelectorAll('[data-codes-container] .recovery-codes__code'),
            function (el) { return el.textContent.trim(); }
        );
    }

    function pad2(n) { return n < 10 ? '0' + n : '' + n; }

    function timestamp() {
        var d = new Date();
        return d.getFullYear() + '-' +
               pad2(d.getMonth() + 1) + '-' +
               pad2(d.getDate());
    }

    function buildPayload(codes) {
        return 'Aspired Websites — Vault Recovery Codes\n' +
               'Generated: ' + timestamp() + '\n' +
               '\n' +
               'Each code can be used ONCE to recover vault access if the\n' +
               'authenticator app is lost. Store these in a password\n' +
               'manager — never inside the vault itself.\n' +
               '\n' +
               codes.join('\n') + '\n';
    }

    // ── Download button ─────────────────────────────────────────────────
    var dl = document.getElementById('download-codes-btn');
    if (dl) {
        dl.addEventListener('click', function () {
            var codes = collectCodes();
            if (!codes.length) { return; }
            var blob = new Blob([buildPayload(codes)],
                                { type: 'text/plain;charset=utf-8' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = 'aspired-vault-recovery-codes-' +
                         timestamp() + '.txt';
            document.body.appendChild(a);
            a.click();
            setTimeout(function () {
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }, 100);
        });
    }

    // ── Copy button ─────────────────────────────────────────────────────
    var copyBtn = document.getElementById('copy-codes-btn');
    if (copyBtn && navigator.clipboard) {
        copyBtn.addEventListener('click', function () {
            var codes = collectCodes();
            if (!codes.length) { return; }
            navigator.clipboard.writeText(codes.join('\n')).then(function () {
                var prev = copyBtn.textContent;
                copyBtn.textContent = 'Copied';
                setTimeout(function () { copyBtn.textContent = prev; }, 1500);
            });
        });
    }

    // ── "I have saved" gate ─────────────────────────────────────────────
    var ack = document.getElementById('codes-saved-ack');
    var cont = document.getElementById('codes-continue-btn');
    if (ack && cont) {
        var sync = function () { cont.disabled = !ack.checked; };
        ack.addEventListener('change', sync);
        sync();
    }
})();
