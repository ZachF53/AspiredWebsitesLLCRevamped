// checkout.js — Stripe Elements integration for the custom checkout
// page. Initialises PaymentElement + AddressElement, validates the
// form, calls our /confirm/ endpoint, handles SCA / 3DS if returned.
//
// CSP requirements: js.stripe.com must be allowed in script-src;
// hooks.stripe.com in frame-src.
(function init() {
    'use strict';

    var btn = document.getElementById('checkout-submit');
    if (!btn || typeof window.Stripe !== 'function') {
        // Stripe.js still loading — retry shortly. Cheap polling
        // avoids racing with the deferred script tag. (Use the named
        // `init` ref — `arguments.callee` throws under 'use strict'.)
        if (btn && typeof window.Stripe !== 'function') {
            setTimeout(init, 100);
        }
        return;
    }

    var publishableKey = btn.getAttribute('data-publishable-key');
    var confirmUrl = btn.getAttribute('data-confirm-url');
    var emailCheckUrl = btn.getAttribute('data-email-check-url');
    if (!publishableKey) return;

    var stripe = Stripe(publishableKey);

    // Dark theme for the Stripe-hosted iframes (PaymentElement +
    // AddressElement). Their internals are cross-origin, so site CSS
    // can't reach them — Stripe's Appearance API is the only lever.
    // Values mirror the checkout page tokens in main.css: panel
    // --color-bg-raised #0F172A, field bg ~#0B101D (black 0.3 over the
    // panel, matching the email input), brand --color-orange #E8650A,
    // text #FFFFFF, muted #94A3B8, font Arial.
    var appearance = {
        theme: 'night',
        variables: {
            colorPrimary: '#E8650A',
            colorBackground: '#0B101D',
            colorText: '#FFFFFF',
            colorTextSecondary: '#94A3B8',
            colorTextPlaceholder: '#64748B',
            colorDanger: '#F87171',
            fontFamily: 'Arial, Helvetica, sans-serif',
            borderRadius: '6px',
            spacingUnit: '4px',
        },
        rules: {
            '.Input': { border: '1px solid rgba(255, 255, 255, 0.12)' },
            '.Input:focus': {
                border: '1px solid #E8650A',
                boxShadow: '0 0 0 1px #E8650A',
            },
            '.Tab': { border: '1px solid rgba(255, 255, 255, 0.12)' },
            '.Tab:hover': { borderColor: 'rgba(232, 101, 10, 0.5)' },
            '.Tab--selected': {
                borderColor: '#E8650A',
                boxShadow: '0 0 0 1px #E8650A',
            },
            '.Label': { color: '#94A3B8' },
        },
    };

    var elements = stripe.elements({
        mode: 'setup',
        currency: 'usd',
        paymentMethodCreation: 'manual',
        appearance: appearance,
    });

    var paymentElement = elements.create('payment');
    paymentElement.mount('#payment-element');

    var addressElement = elements.create('address', { mode: 'billing' });
    addressElement.mount('#address-element');

    var emailEl = document.getElementById('email');
    var emailHint = document.getElementById('email-existing-hint');
    var hostingEl = document.getElementById('hosting-upsell');
    var totalEl = document.getElementById('checkout-total-today');
    var submitAmount = document.getElementById('checkout-submit-amount');
    var errorEl = document.getElementById('checkout-error');
    var form = document.getElementById('checkout-form');

    // Format a number as USD, dropping ".00" on whole-dollar amounts so
    // it matches the server's get_price_display ("$1,199").
    function formatMoney(n) {
        var rounded = Math.round(n * 100) / 100;
        var whole = Math.round(rounded * 100) % 100 === 0;
        return '$' + rounded.toLocaleString('en-US', {
            minimumFractionDigits: whole ? 0 : 2,
            maximumFractionDigits: 2,
        });
    }

    // Hosting upsell — show a REAL combined total (plan + first-year
    // hosting) charged today, instead of the confusing "+ $100". The
    // matching line items are added server-side in checkout_confirm, so
    // this display stays in lock-step with what Stripe actually charges.
    var baseAmount = parseFloat(btn.getAttribute('data-base-amount')) || 0;
    var baseTotalText = totalEl ? totalEl.textContent : '';   // "$1,199/month"
    var baseButtonText = submitAmount ? submitAmount.textContent : '';
    if (hostingEl && totalEl && submitAmount) {
        var hostingAmount = parseFloat(hostingEl.getAttribute('data-amount')) || 0;
        hostingEl.addEventListener('change', function () {
            if (hostingEl.checked) {
                var total = formatMoney(baseAmount + hostingAmount);
                totalEl.textContent = total;
                submitAmount.textContent = total + ' today';
            } else {
                totalEl.textContent = baseTotalText;
                submitAmount.textContent = baseButtonText;
            }
        });
    }

    // Live email-exists check
    if (emailEl) {
        emailEl.addEventListener('blur', function () {
            var email = emailEl.value.trim();
            if (!email) return;
            fetch(emailCheckUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrf(),
                },
                body: JSON.stringify({ email: email }),
            }).then(function (r) { return r.json(); })
              .then(function (data) {
                  if (emailHint) emailHint.hidden = !data.exists;
              }).catch(function () { /* silent */ });
        });
    }

    function getCsrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }

    function showError(msg) {
        errorEl.textContent = msg || 'Something went wrong. Please try again.';
        errorEl.hidden = false;
        btn.disabled = false;
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        errorEl.hidden = true;
        btn.disabled = true;

        var email = emailEl.value.trim();
        if (!email) {
            showError('Please enter your email.');
            return;
        }

        // Validate elements + create a PaymentMethod
        elements.submit().then(function (result) {
            if (result.error) {
                showError(result.error.message);
                return;
            }
            return stripe.createPaymentMethod({
                elements: elements,
                params: { billing_details: { email: email } },
            });
        }).then(function (pmResult) {
            if (!pmResult) return; // already errored
            if (pmResult.error) {
                showError(pmResult.error.message);
                return;
            }
            // POST to our confirm endpoint
            return fetch(confirmUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrf(),
                },
                body: JSON.stringify({
                    email: email,
                    payment_method_id: pmResult.paymentMethod.id,
                    hosting_upsell: hostingEl ? hostingEl.checked : false,
                }),
            }).then(function (r) { return r.json(); })
              .then(function (data) {
                  if (data.error) {
                      showError(data.error);
                      return;
                  }
                  if (data.requires_action) {
                      return stripe.confirmCardPayment(data.client_secret).then(
                          function (result) {
                              if (result.error) {
                                  showError(result.error.message);
                                  return;
                              }
                              window.location.href = data.redirect ||
                                  btn.getAttribute('data-success-url');
                          });
                  }
                  if (data.ok) {
                      window.location.href = data.redirect ||
                          btn.getAttribute('data-success-url');
                  }
              });
        }).catch(function (err) {
            showError((err && err.message) || 'Network error');
        });
    });
})();
