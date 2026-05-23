/* Lead bulk-select + bulk-delete UI. Runs on the leads table page. */
(function () {
    'use strict';

    function init() {
        var form = document.getElementById('lead-bulk-form');
        if (!form) return;

        var toggle = document.getElementById('lead-bulk-toggle');
        var btn = document.getElementById('lead-bulk-btn');
        var count = document.getElementById('lead-bulk-count');
        if (!toggle || !btn || !count) return;

        function rowChecks() {
            return form.querySelectorAll('input.lead-bulk-check');
        }

        function refresh() {
            var sel = form.querySelectorAll(
                'input.lead-bulk-check:checked').length;
            count.textContent = sel;
            btn.disabled = sel === 0;
            // Tri-state on the master toggle.
            var total = rowChecks().length;
            toggle.indeterminate = sel > 0 && sel < total;
            toggle.checked = total > 0 && sel === total;
        }

        toggle.addEventListener('change', function () {
            var want = toggle.checked;
            rowChecks().forEach(function (c) { c.checked = want; });
            refresh();
        });

        // `change` on the form catches per-row checkbox toggles via
        // event bubbling — works for clicks AND keyboard space-bar.
        form.addEventListener('change', function (e) {
            if (e.target && e.target.matches('input.lead-bulk-check')) {
                refresh();
            }
        });

        // Confirm + block submit if nothing checked. Replaces the
        // template's old onsubmit attribute so this works under the
        // strict-CSP admin layout too.
        form.addEventListener('submit', function (e) {
            var n = form.querySelectorAll(
                'input.lead-bulk-check:checked').length;
            if (n === 0) {
                e.preventDefault();
                return;
            }
            var msg = 'Delete ' + n + ' lead' +
                      (n === 1 ? '' : 's') + ' permanently?';
            if (!window.confirm(msg)) {
                e.preventDefault();
            }
        });

        // Initial state.
        refresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
