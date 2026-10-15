/*
 * Site content section rail (/admin-dashboard/v2/site-content/).
 *
 * Eight sections, one visible at a time. The hidden panels stay in the
 * DOM — they are display:none, never detached — so one submit still
 * posts all 35 fields.
 *
 * External file, not inline: /admin-dashboard/ serves CSP_ADMIN_DASHBOARD,
 * whose script-src is 'self' with no 'unsafe-inline'.
 */
(function () {
    'use strict';

    var form = document.getElementById('site-content-form');
    if (!form) { return; }

    var tabs = Array.prototype.slice.call(
        form.querySelectorAll('[data-sc-tab]'));
    var panels = Array.prototype.slice.call(
        form.querySelectorAll('[data-sc-panel]'));
    if (!tabs.length) { return; }

    function show(key, push) {
        var found = false;
        tabs.forEach(function (tab) {
            var on = tab.getAttribute('data-sc-tab') === key;
            tab.classList.toggle('is-active', on);
            tab.setAttribute('aria-selected', on ? 'true' : 'false');
            if (on) { found = true; }
        });
        if (!found) { return false; }
        panels.forEach(function (panel) {
            panel.classList.toggle(
                'is-active', panel.getAttribute('data-sc-panel') === key);
        });
        if (push && window.history && window.history.replaceState) {
            // replaceState, not a hash assignment: setting location.hash
            // scrolls the panel heading under the admin header.
            window.history.replaceState(null, '', '#' + key);
        }
        return true;
    }

    tabs.forEach(function (tab) {
        tab.addEventListener('click', function () {
            show(tab.getAttribute('data-sc-tab'), true);
        });
    });

    // Arrow keys move along the rail, which is what a tablist is
    // expected to do once the buttons take focus.
    form.addEventListener('keydown', function (event) {
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') { return; }
        var index = tabs.indexOf(document.activeElement);
        if (index === -1) { return; }
        event.preventDefault();
        var next = tabs[(index + (event.key === 'ArrowDown' ? 1 : -1)
                         + tabs.length) % tabs.length];
        next.focus();
        show(next.getAttribute('data-sc-tab'), true);
    });

    // A deep link wins over the server's choice, except when the server
    // opened a section because it has errors to fix — the template only
    // marks a tab is-active for that reason after a failed save.
    var hash = (window.location.hash || '').replace('#', '');
    var hasErrors = !!form.querySelector('.sc-rail__item.has-error');
    if (hash && !hasErrors) { show(hash, false); }
}());
