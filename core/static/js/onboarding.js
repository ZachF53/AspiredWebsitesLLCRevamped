/* Legacy-client onboarding board — copy buttons + SSH Key Setup modal.
   CSP-safe: external file, no inline handlers, no eval. */
(function () {
    'use strict';

    function copyToClipboard(text, btn) {
        if (!navigator.clipboard) { return; }
        navigator.clipboard.writeText(text).then(function () {
            var prev = btn.textContent;
            btn.textContent = 'Copied';
            setTimeout(function () { btn.textContent = prev; }, 1500);
        });
    }

    // ── Plain copy buttons (anywhere on the page) ────────────────────────
    document.querySelectorAll('.onboarding-copy[data-copy]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            copyToClipboard(btn.getAttribute('data-copy') || '', btn);
        });
    });

    // ── Modal ────────────────────────────────────────────────────────────
    var modal = document.getElementById('onboarding-modal');
    if (!modal) { return; }

    var firmEls = modal.querySelectorAll('[data-modal-firm]');
    var sshEl = modal.querySelector('[data-modal-ssh]');
    var curlEl = modal.querySelector('[data-modal-curl]');
    var closeBtn = modal.querySelector('.onboarding-modal__close');

    function openModal(ip, firm) {
        var sshCmd = 'ssh root@' + ip;
        var curlCmd = 'curl -s https://aspiredwebsites.com/static/scripts/gen_vault_key.sh | bash';
        firmEls.forEach(function (el) { el.textContent = firm; });
        sshEl.textContent = sshCmd;
        curlEl.textContent = curlCmd;
        modal.hidden = false;
    }

    function closeModal() {
        modal.hidden = true;
    }

    document.querySelectorAll('.onboarding-ssh-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            openModal(
                btn.getAttribute('data-ip') || '',
                btn.getAttribute('data-firm') || '');
        });
    });

    if (closeBtn) { closeBtn.addEventListener('click', closeModal); }
    modal.addEventListener('click', function (e) {
        if (e.target === modal) { closeModal(); }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !modal.hidden) { closeModal(); }
    });

    // Copy buttons inside the modal.
    modal.querySelectorAll('.onboarding-copy[data-copy-target]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var target = btn.getAttribute('data-copy-target');
            var src = (target === 'ssh') ? sshEl : curlEl;
            copyToClipboard(src ? src.textContent : '', btn);
        });
    });
})();
