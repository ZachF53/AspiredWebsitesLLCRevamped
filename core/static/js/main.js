(function () {
    'use strict';

    function initNavToggle() {
        var toggle = document.querySelector('.nav-toggle');
        var menu = document.querySelector('.nav-menu');
        if (!toggle || !menu) return;

        toggle.addEventListener('click', function () {
            var expanded = toggle.getAttribute('aria-expanded') === 'true';
            toggle.setAttribute('aria-expanded', String(!expanded));
            menu.classList.toggle('is-open');
        });

        menu.addEventListener('click', function (e) {
            var target = e.target;
            if (target && target.tagName === 'A') {
                toggle.setAttribute('aria-expanded', 'false');
                menu.classList.remove('is-open');
            }
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && menu.classList.contains('is-open')) {
                toggle.setAttribute('aria-expanded', 'false');
                menu.classList.remove('is-open');
                toggle.focus();
            }
        });
    }

    function initNavDropdowns() {
        // Submenu toggle buttons (Services, etc.). On desktop the
        // submenu opens on hover via pure CSS; this handler covers
        // mobile + keyboard. Click toggles aria-expanded which the
        // CSS uses to show/hide the panel.
        var toggles = document.querySelectorAll('.nav-link--toggle');
        toggles.forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                var open = btn.getAttribute('aria-expanded') === 'true';
                // Close any other open submenu first (single-open policy)
                toggles.forEach(function (other) {
                    if (other !== btn) {
                        other.setAttribute('aria-expanded', 'false');
                    }
                });
                btn.setAttribute('aria-expanded', String(!open));
            });
        });

        // Click outside closes all open submenus
        document.addEventListener('click', function (e) {
            if (e.target.closest('.nav-item--has-children')) return;
            toggles.forEach(function (btn) {
                btn.setAttribute('aria-expanded', 'false');
            });
        });

        // Escape closes
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            toggles.forEach(function (btn) {
                if (btn.getAttribute('aria-expanded') === 'true') {
                    btn.setAttribute('aria-expanded', 'false');
                    btn.focus();
                }
            });
        });
    }

    function initAuditFormLoading() {
        // Show "Analyzing..." state on the audit form so the user knows
        // the ~30s wait isn't a hung browser.
        var form = document.querySelector('.audit-form');
        if (!form) return;
        var btn = form.querySelector('button[type="submit"]');
        var note = form.querySelector('.audit-note');
        if (!btn) return;

        form.addEventListener('submit', function () {
            btn.disabled = true;
            btn.classList.add('is-loading');
            btn.innerHTML =
                '<span class="btn__spinner" aria-hidden="true"></span>' +
                'Analyzing your site…';
            if (note) {
                note.classList.add('audit-note--running');
                note.textContent =
                    'Running PageSpeed audit + AI review — about 30 seconds. ' +
                    'Please don’t refresh or close this tab.';
            }
        });
    }

    function initScrapeFormLoading() {
        // The scrape form blocks for 1-3 minutes — make that obvious.
        var form = document.querySelector('.scrape-form');
        if (!form) return;
        var btn = form.querySelector('button[type="submit"]');
        var note = form.querySelector('.scrape-form__note');
        if (!btn) return;

        form.addEventListener('submit', function () {
            btn.disabled = true;
            btn.classList.add('is-loading');
            btn.innerHTML =
                '<span class="btn__spinner" aria-hidden="true"></span>Scraping…';
            if (note) {
                note.classList.add('audit-note--running');
                note.textContent =
                    'Scraping in progress — this can take 1–3 minutes. ' +
                    'Please don’t refresh or close this tab.';
            }
        });
    }

    function initConfirmActions() {
        // Any element with data-confirm prompts before its action runs —
        // used for destructive buttons (delete, etc.) and plain "are you
        // sure?" confirmations. CSP-safe: no inline JS.
        //
        // Shows an in-app centered modal instead of window.confirm() —
        // the native dialog is browser-chrome-styled (shows the raw
        // hostname, can't be restyled) and reads as a broken/untrusted
        // popup rather than part of the app.
        //
        // data-confirm lives on either a <button> (bubbles to itself) or
        // a <form> (the button's click bubbles up to it) — both already
        // relied on click-bubbling before this change, so this keeps the
        // same trigger surface with zero template edits required.
        var modal = null;

        function buildModal() {
            var el = document.createElement('div');
            el.className = 'confirm-modal';
            el.hidden = true;
            el.innerHTML =
                '<div class="confirm-modal__backdrop"></div>' +
                '<div class="confirm-modal__card" role="alertdialog" aria-modal="true" aria-labelledby="confirm-modal-text">' +
                    '<div class="confirm-modal__body"><p id="confirm-modal-text"></p></div>' +
                    '<div class="confirm-modal__foot">' +
                        '<button type="button" class="btn-secondary btn-sm" data-confirm-cancel>Cancel</button>' +
                        '<button type="button" class="btn-primary btn-sm" data-confirm-ok>OK</button>' +
                    '</div>' +
                '</div>';
            document.body.appendChild(el);
            return el;
        }

        function showConfirmModal(message, onConfirm) {
            if (!modal) { modal = buildModal(); }
            modal.querySelector('#confirm-modal-text').textContent = message;
            modal.hidden = false;

            var okBtn = modal.querySelector('[data-confirm-ok]');
            var cancelBtn = modal.querySelector('[data-confirm-cancel]');
            var backdrop = modal.querySelector('.confirm-modal__backdrop');

            function cleanup() {
                modal.hidden = true;
                okBtn.removeEventListener('click', onOk);
                cancelBtn.removeEventListener('click', onCancel);
                backdrop.removeEventListener('click', onCancel);
                document.removeEventListener('keydown', onKeydown);
            }
            function onOk() { cleanup(); onConfirm(); }
            function onCancel() { cleanup(); }
            function onKeydown(e) {
                if (e.key === 'Escape') { onCancel(); }
            }

            okBtn.addEventListener('click', onOk);
            cancelBtn.addEventListener('click', onCancel);
            backdrop.addEventListener('click', onCancel);
            document.addEventListener('keydown', onKeydown);
            okBtn.focus();
        }

        document.querySelectorAll('[data-confirm]').forEach(function (el) {
            el.addEventListener('click', function (e) {
                if (el.dataset.confirmBypass === '1') {
                    // Re-triggered below after the modal was confirmed —
                    // let it through this time, no loop.
                    delete el.dataset.confirmBypass;
                    return;
                }
                e.preventDefault();
                e.stopPropagation();
                showConfirmModal(el.getAttribute('data-confirm'), function () {
                    el.dataset.confirmBypass = '1';
                    if (el.tagName === 'FORM') {
                        el.submit();
                    } else if (typeof el.click === 'function') {
                        el.click();
                    }
                });
            });
        });
    }

    function initCopyButtons() {
        // [data-copy-target="elementId"] copies that element's value/text.
        document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var target = document.getElementById(
                    btn.getAttribute('data-copy-target'));
                if (!target || !navigator.clipboard) { return; }
                var text = ('value' in target && target.value !== undefined)
                    ? target.value : target.textContent;
                navigator.clipboard.writeText(text).then(function () {
                    var original = btn.textContent;
                    btn.textContent = 'Copied!';
                    setTimeout(function () {
                        btn.textContent = original;
                    }, 1500);
                });
            });
        });
    }

    function ready(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn);
        } else {
            fn();
        }
    }

    ready(initNavToggle);
    ready(initNavDropdowns);
    ready(initAuditFormLoading);
    ready(initScrapeFormLoading);
    ready(initConfirmActions);
    ready(initCopyButtons);
})();
