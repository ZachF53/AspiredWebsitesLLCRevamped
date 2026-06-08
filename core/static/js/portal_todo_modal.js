// portal_todo_modal.js — sidebar To-Do List click → fetch modal partial
// from /onboarding/todos/modal/ and inject into a centered overlay.
// CSP-safe: external script, no inline handlers, no eval.
(function () {
    'use strict';

    var trigger = document.querySelector('[data-todo-trigger]');
    if (!trigger) return;

    var overlay = null;

    function buildOverlay(html) {
        var ov = document.createElement('div');
        ov.className = 'todo-overlay';
        ov.setAttribute('role', 'dialog');
        ov.setAttribute('aria-modal', 'true');
        ov.innerHTML =
            '<div class="todo-overlay__shell">' +
            '  <button type="button" class="todo-overlay__close" aria-label="Close">×</button>' +
            '  <div class="todo-overlay__body">' + html + '</div>' +
            '</div>';
        document.body.appendChild(ov);
        return ov;
    }

    function closeOverlay() {
        if (!overlay) return;
        overlay.remove();
        overlay = null;
        document.body.style.overflow = '';
    }

    function openModal() {
        fetch('/onboarding/todos/modal/', { credentials: 'same-origin' })
            .then(function (r) { return r.text(); })
            .then(function (html) {
                if (overlay) closeOverlay();
                overlay = buildOverlay(html);
                document.body.style.overflow = 'hidden';
                overlay.addEventListener('click', function (e) {
                    if (e.target === overlay) closeOverlay();
                });
                var close = overlay.querySelector('.todo-overlay__close');
                if (close) close.addEventListener('click', closeOverlay);
                document.addEventListener('keydown', escClose);
            })
            .catch(function () { /* silent */ });
    }

    function escClose(e) {
        if (e.key === 'Escape') {
            closeOverlay();
            document.removeEventListener('keydown', escClose);
        }
    }

    trigger.addEventListener('click', function (e) {
        e.preventDefault();
        openModal();
    });
})();
