/*
 * Chat pane behaviour for the AI Employees page.
 *
 * WHY THIS IS AN EXTERNAL FILE
 * /admin-dashboard/ is served with CSP_ADMIN_DASHBOARD, which allows
 * inline STYLES but keeps `script-src 'self'`. An inline <script> block
 * is blocked, and so is htmx's `hx-on:` attribute, which the browser
 * treats as inline script. Both fail silently — the page renders, the
 * behaviour just never happens. Same reason the SSH terminal keeps all
 * its JS external.
 *
 * Everything here is delegated from document.body so it keeps working
 * across htmx swaps, which replace the thread markup repeatedly while a
 * reply is streaming in.
 */
(function () {
    'use strict';

    var THREAD_ID = 'chat-thread';

    function thread() {
        return document.getElementById(THREAD_ID);
    }

    /* Keep the newest text in view. Called on load and after every swap
     * into the thread, so a streaming reply stays pinned to the bottom.
     *
     * Skipped when the operator has scrolled up to read something
     * earlier — yanking the viewport back down mid-read is worse than
     * missing the newest line. 40px of slack absorbs sub-pixel and
     * zoom rounding, which otherwise make "at the bottom" never quite
     * true. */
    function scrollThread(force) {
        var el = thread();
        if (!el) { return; }
        if (!force) {
            var distance = el.scrollHeight - el.scrollTop - el.clientHeight;
            if (distance > 40) { return; }
        }
        el.scrollTop = el.scrollHeight;
    }

    function composer() {
        return document.querySelector('[data-chat-composer]');
    }

    document.addEventListener('DOMContentLoaded', function () {
        scrollThread(true);
        var box = document.querySelector('[data-chat-input]');
        if (box) { box.focus(); }
    });

    /* Enter sends, Shift+Enter makes a new line.
     *
     * requestSubmit() rather than submit(): submit() bypasses both the
     * `required` validation and htmx's submit hook, which would post the
     * form the ordinary way and navigate away from the page. */
    document.body.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || e.shiftKey || e.isComposing) { return; }
        var box = e.target;
        if (!box || !box.matches('[data-chat-input]')) { return; }
        var form = composer();
        if (!form) { return; }
        e.preventDefault();
        if (box.value.trim() === '') { return; }
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
        } else {
            form.dispatchEvent(new Event('submit', {
                bubbles: true, cancelable: true
            }));
        }
    });

    /* Clear the box once the message is actually accepted.
     *
     * On afterRequest, not on submit: clearing optimistically loses what
     * you typed if the POST fails, and retyping it is exactly the moment
     * you would rather still have it. */
    document.body.addEventListener('htmx:afterRequest', function (e) {
        var form = composer();
        if (!form || e.detail.elt !== form) { return; }
        if (!e.detail.successful) { return; }
        form.reset();
        var box = form.querySelector('[data-chat-input]');
        if (box) { box.focus(); }
    });

    document.body.addEventListener('htmx:afterSwap', function (e) {
        if (!e.target) { return; }
        if (e.target.id === THREAD_ID || thread() &&
            thread().contains(e.target)) {
            scrollThread(false);
        }
    });
})();
