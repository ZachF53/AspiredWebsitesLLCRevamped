/*
 * Intake form — wizard navigation + conditional reveals + per-step
 * required-field validation.
 *
 * Wizard:
 *   - One `.intake-step[data-step="N"]` visible at a time.
 *   - Previous + Next buttons in `[data-wizard-nav]`.
 *   - Next disabled until the current step's required fields validate.
 *   - On step 6 (Review), Next is hidden and the separate
 *     `[data-wizard-submit-form]` is shown with its submit button
 *     enabled only when ALL steps pass validation.
 *
 * Required-field rules (markers on the form-group divs):
 *   - data-required          → always required
 *   - data-required-when="<controlId|name>:<value>"
 *                            → required only when the named control
 *                              is set to that value
 *   - data-required-photo-count="N"
 *                            → photo gallery must contain >= N
 *                              uploaded photos (counts .intake-photo-grid__item)
 *   - data-required-file="0|1"
 *                            → file already on the server (set by
 *                              template on the Logo group); paired
 *                              with the file input below
 *
 * Conditional reveals:
 *   - `.intake-reveal[data-reveal-when]` shows/hides based on a
 *     sibling input/select/radio. data-reveal-when can target either
 *     an element id OR a radio-group name (we try both).
 *
 * Strict-CSP-safe: no inline handlers, no eval. Loaded by intake.html
 * with `defer` so the DOM is parsed by the time we run.
 */
(function () {
    'use strict';

    // ───────────────────────────────────────────────────────────────
    // Helpers
    // ───────────────────────────────────────────────────────────────

    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) {
        return Array.from((root || document).querySelectorAll(sel));
    }

    /**
     * Find a control by ID first, then by name. For radio groups we
     * return the FIRST radio in the group; the caller uses .name to
     * query the rest. Returns null if not found.
     */
    function findControl(idOrName) {
        return document.getElementById(idOrName)
            || document.querySelector(
                '[name="' + idOrName.replace(/"/g, '\\"') + '"]');
    }

    /**
     * Read the value of a control (input/select/radio group).
     * Returns '' if unset / nothing selected.
     */
    function readControl(idOrName) {
        var el = findControl(idOrName);
        if (!el) { return ''; }
        if (el.type === 'radio') {
            var picked = document.querySelector(
                'input[name="' + el.name.replace(/"/g, '\\"') +
                '"]:checked');
            return picked ? picked.value : '';
        }
        if (el.type === 'checkbox') {
            return el.checked ? '1' : '';
        }
        return (el.value || '').trim();
    }

    // ───────────────────────────────────────────────────────────────
    // Conditional reveals
    // ───────────────────────────────────────────────────────────────

    function syncReveal(reveal) {
        var controlId = reveal.getAttribute('data-reveal-when');
        var expected = reveal.getAttribute('data-reveal-value');

        var actual = readControl(controlId);
        var shouldReveal;
        if (expected === null) {
            // No data-reveal-value → checkbox-style on/off.
            shouldReveal = !!actual;
        } else {
            shouldReveal = (actual === expected);
        }
        reveal.classList.toggle('is-revealed', shouldReveal);
    }

    function wireReveal(reveal) {
        var controlId = reveal.getAttribute('data-reveal-when');
        var el = findControl(controlId);
        if (!el) { return; }

        // Bind to ALL siblings of a radio group so changing any one
        // re-evaluates. For single inputs, just bind to that input.
        var targets;
        if (el.type === 'radio') {
            targets = $$('input[name="' + el.name.replace(/"/g, '\\"') + '"]');
        } else {
            targets = [el];
        }
        var ev = (el.tagName === 'SELECT' ||
                  el.type === 'radio' || el.type === 'checkbox')
            ? 'change' : 'input';
        targets.forEach(function (t) {
            t.addEventListener(ev, function () {
                syncReveal(reveal);
                // Reveal state change can flip required-field counts.
                revalidate();
            });
        });
        syncReveal(reveal);
    }

    // ───────────────────────────────────────────────────────────────
    // Per-step required-field validation
    // ───────────────────────────────────────────────────────────────

    /**
     * Return true if the form-group's required-rules are satisfied.
     * The group may have several markers — all of them must pass.
     */
    function groupSatisfied(group) {
        // data-required-when="control:expected" — only enforce when
        // the named control matches the expected value.
        var when = group.getAttribute('data-required-when');
        if (when) {
            var parts = when.split(':');
            var actual = readControl(parts[0]);
            if (actual !== parts[1]) {
                // Not in the required state — group is satisfied
                // (and any required-rule below is skipped).
                return true;
            }
        } else if (!group.hasAttribute('data-required')) {
            return true;          // not a required group at all
        }

        // Photo-count rule (Step 2 — at least N photos uploaded).
        var photoCount = group.getAttribute('data-required-photo-count');
        if (photoCount !== null) {
            var n = parseInt(photoCount, 10);
            var photos = $$('.intake-photo-grid__item', group);
            if (photos.length < n) { return false; }
        }

        // File-input rule (Step 1 — Logo). data-required-file="1"
        // means a file is already on the server, so a new upload
        // isn't strictly required. data-required-file="0" means we
        // need a fresh upload.
        var fileFlag = group.getAttribute('data-required-file');
        if (fileFlag !== null) {
            if (fileFlag === '1') { return true; }   // already saved
            var fileInput = $('input[type="file"]', group);
            if (!fileInput || !fileInput.files || !fileInput.files.length) {
                return false;
            }
            return true;          // a fresh file is selected
        }

        // Generic text / textarea / select / radio.
        var inputs = $$(
            'input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]), ' +
            'select, textarea',
            group);
        for (var i = 0; i < inputs.length; i++) {
            if (!inputs[i].value || !inputs[i].value.trim()) {
                return false;
            }
        }

        // Radio groups inside this form-group — at least one must be
        // selected.
        var radioGroups = {};
        $$('input[type="radio"]', group).forEach(function (r) {
            radioGroups[r.name] = radioGroups[r.name] || false;
            if (r.checked) { radioGroups[r.name] = true; }
        });
        for (var name in radioGroups) {
            if (!radioGroups[name]) { return false; }
        }

        return true;
    }

    /**
     * Return true if every required group inside `step` is satisfied.
     */
    function stepValid(step) {
        var groups = $$(
            '[data-required], [data-required-when]', step);
        return groups.every(groupSatisfied);
    }

    /**
     * Return true if EVERY step passes — used to enable the final
     * Submit button on step 6.
     */
    function allStepsValid() {
        return $$('.intake-step[data-step]').every(stepValid);
    }

    // ───────────────────────────────────────────────────────────────
    // Wizard
    // ───────────────────────────────────────────────────────────────

    var current = 1;
    var total = 1;

    function showStep(n) {
        $$('.intake-step[data-step]').forEach(function (s) {
            var stepNum = parseInt(s.getAttribute('data-step'), 10);
            s.classList.toggle('is-current', stepNum === n);
        });
        current = n;
        var label = $('[data-wizard-current]');
        if (label) { label.textContent = String(n); }
        revalidate();
        // Scroll the page top so the new step's heading is in view.
        if (typeof window.scrollTo === 'function') {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
    }

    function revalidate() {
        var prevBtn = $('[data-wizard-prev]');
        var nextBtn = $('[data-wizard-next]');
        var submitForm = $('[data-wizard-submit-form]');
        var submitBtn = $('[data-wizard-submit-btn]');
        var submitHint = $('[data-wizard-submit-hint]');

        if (prevBtn) { prevBtn.disabled = (current <= 1); }

        if (current >= total) {
            // Last step — show submit form, hide Next.
            if (nextBtn) { nextBtn.style.display = 'none'; }
            if (submitForm) { submitForm.style.display = ''; }
            var ok = allStepsValid();
            if (submitBtn) { submitBtn.disabled = !ok; }
            if (submitHint) { submitHint.style.display = ok ? 'none' : ''; }
        } else {
            if (nextBtn) {
                nextBtn.style.display = '';
                var step = document.querySelector(
                    '.intake-step[data-step="' + current + '"]');
                nextBtn.disabled = !(step && stepValid(step));
            }
            if (submitForm) { submitForm.style.display = 'none'; }
        }
    }

    function wireWizard() {
        var steps = $$('.intake-step[data-step]');
        if (!steps.length) { return; }

        total = steps.length;
        var totalEl = $('[data-wizard-total]');
        if (totalEl) { totalEl.textContent = String(total); }

        // Conditional reveals first — they affect required-field
        // counts the validator needs to know about.
        $$('.intake-reveal[data-reveal-when]').forEach(wireReveal);

        var prevBtn = $('[data-wizard-prev]');
        var nextBtn = $('[data-wizard-next]');

        if (prevBtn) {
            prevBtn.addEventListener('click', function () {
                if (current > 1) { showStep(current - 1); }
            });
        }
        if (nextBtn) {
            nextBtn.addEventListener('click', function () {
                if (current < total) { showStep(current + 1); }
            });
        }

        // Re-validate on every input change anywhere in the form.
        var form = $('form.intake-form');
        if (form) {
            form.addEventListener('input', revalidate);
            form.addEventListener('change', revalidate);
        }

        // Also re-validate when the photo gallery swaps (HTMX upload/
        // delete). HTMX fires `htmx:afterSwap` on the swap target.
        document.body.addEventListener('htmx:afterSwap', function () {
            revalidate();
        });

        showStep(1);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wireWizard);
    } else {
        wireWizard();
    }
})();
