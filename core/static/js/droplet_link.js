/**
 * Droplets list — "Link to Website" modal wiring.
 *
 * External per CSP (script-src 'self'). Loaded by droplets_list.html.
 *
 * Each unlinked droplet row has a button:
 *   <button class="droplet-link-btn"
 *           data-droplet-id="123"
 *           data-droplet-name="foo"
 *           data-droplet-ip="1.2.3.4">Link to Website</button>
 *
 * Clicking it:
 *   - Reads the data-* attrs.
 *   - Sets the modal's form action to
 *     /admin-dashboard/droplets/<id>/link-to-website/.
 *   - Populates the read-only droplet name + IP in the modal body.
 *   - Shows the modal.
 *
 * Confirm submits the form (server validates the website pick,
 * clears any prior linkage, writes the new one). Cancel / backdrop /
 * Esc closes without submitting.
 */
(function () {
    var modal = document.getElementById('link-droplet-modal');
    var backdrop = document.getElementById('link-droplet-backdrop');
    var cancelBtn = document.getElementById('link-droplet-cancel');
    var confirmBtn = document.getElementById('link-droplet-confirm');
    var form = document.getElementById('link-droplet-form');
    var select = document.getElementById('link-droplet-website-select');
    var nameSpan = document.getElementById('link-droplet-name');
    var ipSpan = document.getElementById('link-droplet-ip');

    if (!modal || !form || !confirmBtn) { return; }

    function show(dropletId, dropletName, dropletIp) {
        form.action = '/admin-dashboard/droplets/' + dropletId
            + '/link-to-website/';
        if (nameSpan) { nameSpan.textContent = dropletName || '(unnamed)'; }
        if (ipSpan) { ipSpan.textContent = dropletIp || '(no IP)'; }
        if (select) { select.value = ''; }
        modal.hidden = false;
        document.body.style.overflow = 'hidden';
        setTimeout(function () {
            if (select) { select.focus(); }
        }, 0);
    }

    function hide() {
        modal.hidden = true;
        document.body.style.overflow = '';
    }

    // Delegate — the droplet table is re-rendered every 30s via the
    // HTMX poll, so direct .addEventListener on buttons would die
    // after the first poll. Body-level click delegation survives
    // re-renders.
    document.body.addEventListener('click', function (e) {
        var btn = e.target && e.target.closest('.droplet-link-btn');
        if (!btn) { return; }
        e.preventDefault();
        show(
            btn.getAttribute('data-droplet-id'),
            btn.getAttribute('data-droplet-name'),
            btn.getAttribute('data-droplet-ip'));
    });

    if (cancelBtn) { cancelBtn.addEventListener('click', hide); }
    if (backdrop) { backdrop.addEventListener('click', hide); }
    confirmBtn.addEventListener('click', function () {
        if (!select || !select.value) {
            if (select) { select.reportValidity(); }
            return;
        }
        if (form.requestSubmit) { form.requestSubmit(); }
        else { form.submit(); }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !modal.hidden) { hide(); }
    });
})();
