/* Scan dashboard JS — Run-New-Scan modal: open/close, URL/IP preview
   syncs from the selected client. CSP-safe: external file, no inline
   handlers, no eval. */
(function () {
    'use strict';

    var modal = document.getElementById('run-scan-modal');
    if (!modal) { return; }

    var openBtn   = document.getElementById('open-run-scan-modal');
    var closeBtn  = document.getElementById('close-run-scan-modal');
    var cancelBtn = document.getElementById('run-scan-cancel');
    var clientSel = document.getElementById('run-scan-client');
    var urlOut    = document.getElementById('run-scan-url-preview');
    var ipOut     = document.getElementById('run-scan-ip-preview');
    var submitBtn = document.getElementById('run-scan-submit');
    var form      = modal.querySelector('form');

    function openModal() {
        modal.hidden = false;
        if (clientSel) { clientSel.focus(); }
    }
    function closeModal() { modal.hidden = true; }

    if (openBtn)   { openBtn.addEventListener('click', openModal); }
    if (closeBtn)  { closeBtn.addEventListener('click', closeModal); }
    if (cancelBtn) { cancelBtn.addEventListener('click', closeModal); }
    modal.addEventListener('click', function (e) {
        if (e.target === modal) { closeModal(); }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !modal.hidden) { closeModal(); }
    });

    function syncPreviews() {
        if (!clientSel) { return; }
        var opt = clientSel.options[clientSel.selectedIndex];
        var url = opt && opt.getAttribute('data-url');
        var ip  = opt && opt.getAttribute('data-ip');
        if (urlOut) { urlOut.textContent = url || '—'; }
        if (ipOut)  { ipOut.textContent  = ip  || '—'; }
    }
    if (clientSel) {
        clientSel.addEventListener('change', syncPreviews);
        syncPreviews();
    }

    if (form && submitBtn) {
        form.addEventListener('submit', function () {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Starting…';
        });
    }
})();
