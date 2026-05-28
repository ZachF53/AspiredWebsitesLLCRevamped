/**
 * Scrape form — show/hide the custom-niche text input based on the
 * practice-area dropdown selection.
 *
 * External per CSP (script-src 'self'). Loaded by
 * admin_dashboard/scrape.html.
 *
 * Wiring:
 *   <select data-niche-select="true"> ... <option value="__custom__">
 *   <div data-niche-custom-group="true" hidden>
 *     <input data-niche-custom="true">
 *
 * Behaviour:
 *   - Picking "Other (custom search)" reveals the group + focuses
 *     the input + flips it to required.
 *   - Picking anything else hides the group + clears required so
 *     submit isn't blocked by an empty hidden field.
 *   - Initial render: server template already toggles the [hidden]
 *     attribute, but we re-evaluate on DOM-ready to handle the
 *     edge where the page loads with "Other" pre-selected via
 *     browser back/forward or autofill.
 */
(function () {
    var select = document.querySelector('select[data-niche-select="true"]');
    var group = document.querySelector('[data-niche-custom-group="true"]');
    var input = document.querySelector('input[data-niche-custom="true"]');

    if (!select || !group || !input) { return; }

    var SENTINEL = '__custom__';

    function sync() {
        var isCustom = (select.value === SENTINEL);
        if (isCustom) {
            group.removeAttribute('hidden');
            input.required = true;
        } else {
            group.setAttribute('hidden', '');
            input.required = false;
        }
    }

    select.addEventListener('change', function () {
        sync();
        if (select.value === SENTINEL) {
            // Defer so the unhidden group has paint-mounted before
            // we steal focus.
            setTimeout(function () { input.focus(); }, 0);
        }
    });

    sync();
})();
