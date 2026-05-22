/* Portal Activity Log — the month filter submits its form on change.
   CSP-safe: external file, no inline handlers. */
(function () {
    'use strict';
    document.querySelectorAll('select[data-autosubmit]').forEach(function (sel) {
        sel.addEventListener('change', function () {
            var form = sel.closest('form');
            if (form) { form.submit(); }
        });
    });
})();
