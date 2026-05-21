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

    function ready(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn);
        } else {
            fn();
        }
    }

    ready(initNavToggle);
    ready(initAuditFormLoading);
    ready(initScrapeFormLoading);
})();
