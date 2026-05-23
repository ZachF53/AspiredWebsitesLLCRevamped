/* Droplet dashboard — copy IPs, live cost + tags preview on the spin-up
   form. CSP-safe: external file, no inline handlers. */
(function () {
    'use strict';

    // ── Copy IP buttons (delegated so HTMX re-renders still work) ────────
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.droplet-copy-btn');
        if (!btn) { return; }
        var text = btn.getAttribute('data-copy') || '';
        if (!text || !navigator.clipboard) { return; }
        navigator.clipboard.writeText(text).then(function () {
            var prev = btn.textContent;
            btn.textContent = 'Copied';
            setTimeout(function () { btn.textContent = prev; }, 1500);
        });
    });

    // ── Spin-up form: cost + tags preview ────────────────────────────────
    var sizeRadios = document.querySelectorAll('input[name="size"]');
    var clientSelect = document.getElementById('droplet-client');
    var costPreview = document.getElementById('droplet-cost-preview');
    var tagsPreview = document.getElementById('droplet-tags-preview');

    function currentPrice() {
        var checked = document.querySelector('input[name="size"]:checked');
        return checked ? checked.getAttribute('data-price') : '0';
    }

    function syncCost() {
        if (!costPreview) { return; }
        costPreview.textContent = '$' + currentPrice() + '/mo';
    }

    function syncTags() {
        if (!tagsPreview || !clientSelect) { return; }
        var hasClient = !!clientSelect.value;
        tagsPreview.textContent = hasClient
            ? 'aspired-websites, client'
            : 'aspired-websites, manual';
    }

    if (sizeRadios.length) {
        sizeRadios.forEach(function (r) { r.addEventListener('change', syncCost); });
        syncCost();
    }
    if (clientSelect) {
        clientSelect.addEventListener('change', syncTags);
        syncTags();
    }

    // ── Spin-up form: disable submit so impatient clicks don't double-fire ─
    var submitBtn = document.getElementById('droplet-submit');
    var form = submitBtn && submitBtn.closest('form');
    if (form && submitBtn) {
        form.addEventListener('submit', function () {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Provisioning…';
        });
    }

    // ── Destroy form: arm the button only when the name matches ──────────
    var destroyForm = document.querySelector('.droplet-destroy-form');
    if (destroyForm) {
        var input = destroyForm.querySelector('input[name="confirm_name"]');
        var confirmBtn = destroyForm.querySelector('.droplet-destroy-confirm-btn');
        var target = input && input.getAttribute('placeholder');
        if (input && confirmBtn && target) {
            var sync = function () {
                confirmBtn.disabled = input.value.trim() !== target;
                confirmBtn.classList.toggle(
                    'droplet-destroy-confirm-btn--armed',
                    !confirmBtn.disabled);
            };
            input.addEventListener('input', sync);
            sync();
        }
    }
})();
