/*
 * portal_readonly.js — disables portal actions during a staff
 * "view as client" session.
 *
 * Loaded from clients/base.html only when `impersonating` is true.
 *
 * THIS IS COSMETIC. The real read-only boundary is
 * core.middleware.ImpersonationGuardMiddleware, which refuses every
 * state-changing portal request server-side. The operator is still
 * authenticated as themselves with a valid session and a valid CSRF
 * token, so anything re-enabled in devtools still gets a 403. The point
 * here is to show the portal in the state the client sees it without
 * arming the buttons, so nothing fires by reflex.
 *
 * Opt out of disabling with `data-readonly-allow` — the Exit button in
 * the banner carries it.
 */
(function () {
    'use strict';

    var ALLOW = '[data-readonly-allow]';
    var TITLE = 'Disabled — read-only view-as session';

    function allowed(el) {
        return el.closest(ALLOW) !== null;
    }

    function disableControls(root) {
        var controls = root.querySelectorAll(
            'button, input[type="submit"], input[type="button"], ' +
            'input[type="reset"]'
        );
        Array.prototype.forEach.call(controls, function (el) {
            if (allowed(el) || el.disabled) {
                return;
            }
            el.disabled = true;
            el.setAttribute('aria-disabled', 'true');
            el.setAttribute('title', TITLE);
            el.classList.add('is-readonly');
        });
    }

    function markForms(root) {
        var forms = root.querySelectorAll('form');
        Array.prototype.forEach.call(forms, function (form) {
            if (allowed(form)) {
                return;
            }
            form.classList.add('is-readonly');
        });
    }

    function init(root) {
        disableControls(root);
        markForms(root);
    }

    document.addEventListener('DOMContentLoaded', function () {
        init(document);
    });

    /*
     * Capture-phase submit blocker.
     *
     * Disabling the submit button is not enough on its own: a form can
     * still be submitted by pressing Enter in a text input, and several
     * portal forms are driven by htmx rather than by a submit button at
     * all. Capture phase so this runs before any handler that might
     * call the request itself.
     */
    document.addEventListener('submit', function (evt) {
        if (evt.target && !allowed(evt.target)) {
            evt.preventDefault();
            evt.stopPropagation();
        }
    }, true);

    /*
     * htmx swaps in new markup after load, which would arrive with live
     * buttons. Re-run over each swapped fragment.
     */
    document.body.addEventListener('htmx:afterSwap', function (evt) {
        if (evt.target && evt.target.querySelectorAll) {
            init(evt.target);
        }
    });

    /*
     * Also block htmx requests outright. htmx fires non-GET verbs from
     * hx-post / hx-delete attributes on plain elements that are neither
     * buttons nor inside a form, so neither of the two guards above
     * would see them.
     */
    document.body.addEventListener('htmx:beforeRequest', function (evt) {
        var verb = (evt.detail && evt.detail.requestConfig
            && evt.detail.requestConfig.verb) || 'get';
        if (verb.toLowerCase() === 'get') {
            return;
        }
        if (evt.detail.elt && allowed(evt.detail.elt)) {
            return;
        }
        evt.preventDefault();
    });
}());
