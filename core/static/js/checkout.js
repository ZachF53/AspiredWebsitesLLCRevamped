// checkout.js — Stripe Elements integration for the custom checkout
// page. Initialises PaymentElement + AddressElement, validates the
// form, calls our /confirm/ endpoint, handles SCA / 3DS if returned.
//
// CSP requirements: js.stripe.com must be allowed in script-src;
// hooks.stripe.com in frame-src.
(function () {
    'use strict';

    var btn = document.getElementById('checkout-submit');
    if (!btn || typeof window.Stripe !== 'function') {
        // Stripe.js still loading — retry shortly. Cheap polling
        // avoids racing with the deferred script tag.
        if (btn && typeof window.Stripe !== 'function') {
            setTimeout(arguments.callee, 100);
        }
        return;
    }

    var publishableKey = btn.getAttribute('data-publishable-key');
    var confirmUrl = btn.getAttribute('data-confirm-url');
    var emailCheckUrl = btn.getAttribute('data-email-check-url');
    if (!publishableKey) return;

    var stripe = Stripe(publishableKey);
    var elements = stripe.elements({
        mode: 'setup',
        currency: 'usd',
        paymentMethodCreation: 'manual',
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

    // Hosting upsell — visually update the total. Actual line items
    // are added server-side in checkout_confirm.
    var baseTotalText = totalEl ? totalEl.textContent : '';
    if (hostingEl) {
        hostingEl.addEventListener('change', function () {
            if (hostingEl.checked) {
                totalEl.textContent = baseTotalText + ' + $100 (hosting)';
                submitAmount.textContent = baseTotalText + ' + $100';
            } else {
                totalEl.textContent = baseTotalText;
                submitAmount.textContent = baseTotalText;
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
