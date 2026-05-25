/**
 * Account detail page — delete-account modal with type-to-unlock.
 *
 * External because the site CSP is `script-src 'self'` — no inline.
 *
 * Behaviour:
 *   - Click "Delete account" → modal opens.
 *   - The big red "Delete account permanently" button starts disabled.
 *   - As the admin types in the confirm input, it case-insensitively
 *     matches against data-expected-name (the account name lowered).
 *     On match, the button enables; if they edit past it, it
 *     re-disables.
 *   - Confirm submits the form (server re-checks the name, so a
 *     crafted POST can't skip).
 *   - Cancel / backdrop click / ESC closes the modal AND resets
 *     the input + button state so a re-open starts fresh.
 */
(function () {
    var openBtn = document.getElementById('open-delete-account-modal');
    var modal = document.getElementById('delete-account-modal');
    var backdrop = document.getElementById('delete-account-backdrop');
    var cancelBtn = document.getElementById('cancel-delete-account-modal');
    var confirmBtn = document.getElementById('confirm-delete-account-modal');
    var form = document.getElementById('delete-account-form');
    var input = document.getElementById('delete-account-confirm-input');

    if (!openBtn || !modal || !form || !input || !confirmBtn) { return; }

    var expected = (input.getAttribute('data-expected-name') || '').trim();

    function reset() {
        input.value = '';
        confirmBtn.disabled = true;
    }

    function show() {
        reset();
        modal.hidden = false;
        document.body.style.overflow = 'hidden';
        // Defer focus so the modal is paint-mounted first.
        setTimeout(function () { input.focus(); }, 0);
    }

    function hide() {
        modal.hidden = true;
        document.body.style.overflow = '';
        reset();
    }

    function refreshButton() {
        var typed = (input.value || '').trim().toLowerCase();
        confirmBtn.disabled = !(expected && typed === expected);
    }

    openBtn.addEventListener('click', show);
    if (cancelBtn) { cancelBtn.addEventListener('click', hide); }
    if (backdrop) { backdrop.addEventListener('click', hide); }
    input.addEventListener('input', refreshButton);
    confirmBtn.addEventListener('click', function () {
        if (confirmBtn.disabled) { return; }
        if (form.requestSubmit) { form.requestSubmit(); }
        else { form.submit(); }
    });
    // Enter inside the input also submits — only when the button is
    // armed. Prevents accidental submit while the name is half-typed.
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (!confirmBtn.disabled) { confirmBtn.click(); }
        }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !modal.hidden) { hide(); }
    });
})();
