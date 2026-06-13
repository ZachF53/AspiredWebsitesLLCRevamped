/**
 * Website detail page — move-to-different-account confirm modal.
 *
 * The site CSP is `script-src 'self'` — inline <script> blocks are
 * silently blocked, so this lives in an external file loaded via
 * <script src="..." defer> in admin_dashboard/website_detail.html.
 *
 * Behaviour:
 *  - Click "Move to selected account" → if no destination is picked
 *    yet, show the select's native required-field validity message
 *    and stop. Otherwise show the confirm modal.
 *  - Modal shows the source + destination account names so the admin
 *    sees exactly what they're about to do.
 *  - "Yes, move it" triggers the real form submit via
 *    form.requestSubmit() (modern) with .submit() fallback.
 *  - Cancel button, backdrop click, and Escape key all close the
 *    modal without submitting.
 */
(function () {
    var openBtn = document.getElementById('open-move-modal');
    var modal = document.getElementById('move-account-modal');
    var backdrop = document.getElementById('move-account-backdrop');
    var cancelBtn = document.getElementById('cancel-move-modal');
    var confirmBtn = document.getElementById('confirm-move-modal');
    var form = document.getElementById('move-account-form');
    var select = document.getElementById('move_account_id');
    var targetName = document.getElementById('move-account-target-name');

    // Bail silently if any expected element is missing — page may have
    // been rendered without the danger card (e.g. an unmigrated env).
    if (!openBtn || !modal || !form || !select) { return; }

    function show() {
        var opt = select.options[select.selectedIndex];
        if (!opt || !opt.value) {
            // Use the native required-field bubble — user hasn't picked.
            select.reportValidity();
            return;
        }
        if (targetName) {
            targetName.textContent = opt.getAttribute('data-name') || opt.text;
        }
        modal.hidden = false;
        document.body.style.overflow = 'hidden';
    }

    function hide() {
        modal.hidden = true;
        document.body.style.overflow = '';
    }

    openBtn.addEventListener('click', show);
    if (cancelBtn) { cancelBtn.addEventListener('click', hide); }
    if (backdrop) { backdrop.addEventListener('click', hide); }
    if (confirmBtn) {
        confirmBtn.addEventListener('click', function () {
            if (form.requestSubmit) { form.requestSubmit(); }
            else { form.submit(); }
        });
    }
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !modal.hidden) { hide(); }
    });
})();

/**
 * Monitoring accordion — lazy-load each tool's iframe the first time
 * its <details> panel is opened. The iframe carries the real URL on
 * data-src and stays empty until needed, so opening the page doesn't
 * fire six embedded page loads at once.
 */
(function () {
    var tools = document.querySelectorAll('details.wd-tool');
    Array.prototype.forEach.call(tools, function (d) {
        d.addEventListener('toggle', function () {
            if (!d.open) { return; }
            var frame = d.querySelector('iframe.wd-tool__frame');
            if (frame && !frame.src && frame.dataset.src) {
                frame.src = frame.dataset.src;
            }
        });
    });
})();

/**
 * Copy-to-clipboard for elements flagged with data-copy-target="<id>".
 * Copies that element's text and briefly flips the button label.
 */
(function () {
    var btns = document.querySelectorAll('[data-copy-target]');
    Array.prototype.forEach.call(btns, function (btn) {
        btn.addEventListener('click', function () {
            var el = document.getElementById(btn.dataset.copyTarget);
            if (!el) { return; }
            var text = el.textContent || '';
            var done = function () {
                var orig = btn.textContent;
                btn.textContent = 'Copied';
                setTimeout(function () { btn.textContent = orig; }, 1500);
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, done);
            } else {
                var ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand('copy'); } catch (e) {}
                document.body.removeChild(ta);
                done();
            }
        });
    });
})();
