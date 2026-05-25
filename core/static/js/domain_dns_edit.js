/**
 * DNS-record editor — add/remove rows.
 *
 * External file because the portal CSP is `script-src 'self'` (no
 * inline JS allowed). Hooks into:
 *   - #dns-rows           — tbody where rows live
 *   - #dns-row-template   — hidden <template> with the blank row markup
 *   - #add-record         — the "+ Add record" button
 *   - [data-remove]       — per-row delete buttons inside #dns-rows
 */
(function () {
    'use strict';

    var tbody = document.getElementById('dns-rows');
    var tmpl  = document.getElementById('dns-row-template');
    var addBtn = document.getElementById('add-record');

    if (!tbody || !tmpl || !addBtn) {
        return;
    }

    addBtn.addEventListener('click', function () {
        var clone = tmpl.content.cloneNode(true);
        tbody.appendChild(clone);
    });

    tbody.addEventListener('click', function (e) {
        var remove = e.target.closest('[data-remove]');
        if (!remove) {
            return;
        }
        var row = remove.closest('tr');
        if (row) {
            row.remove();
        }
    });
})();
