/**
 * v2 website detail — Billing tab, Add Plan form.
 *
 * The site CSP is `script-src 'self'` — inline <script> blocks are
 * silently blocked, so this lives in an external file loaded via
 * <script src="..." defer> in admin_dashboard/v2/website_detail.html.
 *
 * Behaviour:
 *  - Live price preview (list price, discount, first charge/invoice)
 *    as the operator picks a tier/discount. Client-side only — a
 *    preview, not the charge. The server (start_website_plan) is the
 *    authority on what Stripe actually bills.
 *  - Selecting a service type that already has an active plan shows the
 *    matching blocked banner and disables Review/Submit — this creates
 *    a real Stripe subscription, so the guard has to be visible, not a
 *    silent no-op.
 *  - "Review" builds a one-line confirmation (tier, amount, discount,
 *    duration, which Stripe branch fires) and only THEN reveals the
 *    real submit button + flips the hidden `confirmed` field to "yes".
 *    Changing any field after Review hides the confirmation again and
 *    requires a fresh Review — no stale confirmation can ride through
 *    on a since-changed selection.
 */
(function () {
    var form = document.getElementById('ap-form');
    if (!form) { return; }

    var serviceTypeSelect = document.getElementById('ap-service-type');
    var tierSelect = document.getElementById('ap-tier');
    var pctInput = document.getElementById('ap-discount-pct');
    var durationSelect = document.getElementById('ap-discount-duration');

    var blockedMaintenance = document.getElementById('ap-blocked-maintenance');
    var blockedSocial = document.getElementById('ap-blocked-social');

    var previewList = document.getElementById('ap-preview-list');
    var previewDiscount = document.getElementById('ap-preview-discount');
    var previewTotal = document.getElementById('ap-preview-total');
    var previewNote = document.getElementById('ap-preview-note');

    var confirmBox = document.getElementById('ap-confirm');
    var confirmText = document.getElementById('ap-confirm-text');
    var confirmedField = document.getElementById('ap-confirmed');
    var reviewBtn = document.getElementById('ap-review-btn');
    var submitBtn = document.getElementById('ap-submit-btn');

    var hasCard = form.getAttribute('data-has-card') === '1';
    var cardLabel = form.getAttribute('data-card-label') || '';

    function money(n) {
        return '$' + n.toFixed(2);
    }

    function selectedTierOption() {
        return tierSelect.options[tierSelect.selectedIndex];
    }

    function isBlocked() {
        var opt = serviceTypeSelect.options[serviceTypeSelect.selectedIndex];
        return !!(opt && opt.getAttribute('data-blocked'));
    }

    function resetConfirm() {
        confirmedField.value = '';
        if (confirmBox) { confirmBox.hidden = true; }
        if (submitBtn) { submitBtn.hidden = true; }
    }

    function updateBlockedState() {
        var type = serviceTypeSelect.value;
        if (blockedMaintenance) { blockedMaintenance.hidden = (type !== 'maintenance'); }
        if (blockedSocial) { blockedSocial.hidden = (type !== 'social'); }
        var blocked = isBlocked();
        if (reviewBtn) { reviewBtn.disabled = blocked; }
        if (blocked) { resetConfirm(); }
    }

    function updatePreview() {
        var opt = selectedTierOption();
        var priceRaw = opt ? opt.getAttribute('data-price') : '';
        var pctRaw = pctInput.value.trim();
        var pct = pctRaw ? parseInt(pctRaw, 10) : 0;
        if (isNaN(pct) || pct < 0) { pct = 0; }
        if (pct > 100) { pct = 100; }

        if (!priceRaw) {
            previewList.textContent = '—';
            previewDiscount.textContent = '—';
            previewTotal.textContent = '—';
            previewNote.hidden = true;
            return;
        }

        var price = parseFloat(priceRaw);
        var discounted = pct ? price * (1 - pct / 100) : price;

        previewList.textContent = money(price) + '/mo';
        previewDiscount.textContent = pct ? (pct + '% off') : 'None';
        previewTotal.textContent = money(discounted) + (hasCard ? ' (charged now)' : ' (once they add a card)');

        if (pct && durationSelect.value === 'once') {
            previewNote.textContent = 'First month only — reverts to ' + money(price) + '/mo starting month 2.';
            previewNote.hidden = false;
        } else {
            previewNote.hidden = true;
        }
    }

    function buildConfirmText() {
        var opt = selectedTierOption();
        var tierName = opt ? (opt.getAttribute('data-name') || opt.text) : '';
        var priceRaw = opt ? opt.getAttribute('data-price') : '';
        var pctRaw = pctInput.value.trim();
        var pct = pctRaw ? parseInt(pctRaw, 10) : 0;
        var durationLabel = durationSelect.options[durationSelect.selectedIndex].text;

        var amountTxt = '';
        if (priceRaw) {
            var price = parseFloat(priceRaw);
            var discounted = pct ? price * (1 - pct / 100) : price;
            amountTxt = money(discounted) + '/mo';
        }

        var branchTxt = hasCard
            ? 'charge the card on file (' + cardLabel + ') immediately'
            : 'email them a secure link to our own payment page — nothing charges today, no Stripe subscription exists until they add a card';

        var discountTxt = pct ? (pct + '% off, ' + durationLabel.toLowerCase()) : 'no discount';

        return 'About to add ' + tierName + ' at ' + amountTxt + ' (' + discountTxt
            + '). This will ' + branchTxt + '. Confirm to proceed.';
    }

    if (reviewBtn) {
        reviewBtn.addEventListener('click', function () {
            if (isBlocked()) { return; }
            if (!tierSelect.value) {
                tierSelect.reportValidity();
                return;
            }
            var pctRaw = pctInput.value.trim();
            if (pctRaw) {
                var pct = parseInt(pctRaw, 10);
                if (isNaN(pct) || pct < 1 || pct > 100) {
                    pctInput.reportValidity();
                    return;
                }
            }
            confirmText.textContent = buildConfirmText();
            confirmBox.hidden = false;
            confirmedField.value = 'yes';
            submitBtn.hidden = false;
        });
    }

    [serviceTypeSelect, tierSelect, pctInput, durationSelect].forEach(function (el) {
        if (!el) { return; }
        el.addEventListener('change', function () {
            updateBlockedState();
            updatePreview();
            resetConfirm();
        });
        el.addEventListener('input', function () {
            updatePreview();
            resetConfirm();
        });
    });

    updateBlockedState();
    updatePreview();
})();
