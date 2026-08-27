/**
 * New-invoice form — reveal the custom-amount input only when the
 * "Custom amount" package option is selected.
 *
 * This lives in a static file rather than an inline <script> because
 * /admin-dashboard/ serves CSP_ADMIN_DASHBOARD, which inherits
 * `script-src 'self'` from CSP_PUBLIC. That blocks an inline <script>
 * block outright, not just inline handlers — so the row stayed
 * display:none forever and a custom-priced invoice could not be
 * entered from the UI at all. The backend accepted `custom_amount`
 * the whole time; only the field was unreachable.
 */
(function () {
    function init() {
        var sel = document.getElementById('package-select');
        var row = document.getElementById('custom-amount-row');
        if (!sel || !row) { return; }

        function sync() {
            row.style.display = (sel.value === 'custom') ? '' : 'none';
        }

        sel.addEventListener('change', sync);
        // Run once on load so a re-rendered form (validation error)
        // comes back with the field already open.
        sync();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
