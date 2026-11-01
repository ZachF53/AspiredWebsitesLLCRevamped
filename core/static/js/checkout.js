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
    // Presence of the checkbox is what marks a trial checkout, so there
    // is no need to read data-is-trial here. The attribute stays on the
    // button for the tests and for anything that needs to know without
    // depending on a specific element existing.
    var termsEl = document.getElementById('terms-accept');

    // Terms gate. The button ships disabled from the server on a trial
    // checkout and only this listener enables it, so a buyer with JS
    // broken gets a dead button rather than a live one that skips the
    // acceptance. checkout_confirm re-checks server-side regardless —
    // this is the courtesy, not the control.
    if (termsEl) {
        termsEl.addEventListener('change', function () {
            btn.disabled = !termsEl.checked;
        });
    }

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

    // Button loading state — a spinner + "Processing…" so the buyer
    // knows the click registered while we talk to Stripe (this can take
    // a couple of seconds). Restored on any error; on success we
    // navigate away so it just stays spinning.
    var loadingRestoreHtml = null;
    function startLoading() {
        loadingRestoreHtml = btn.innerHTML;
        btn.disabled = true;
        btn.classList.add('is-loading');
        btn.innerHTML =
            '<span class="btn-spinner" aria-hidden="true"></span>Processing…';
    }
    function stopLoading() {
        btn.disabled = false;
        btn.classList.remove('is-loading');
        if (loadingRestoreHtml !== null) {
            btn.innerHTML = loadingRestoreHtml;
            loadingRestoreHtml = null;
        }
    }

    function showError(msg) {
        errorEl.textContent = msg || 'Something went wrong. Please try again.';
        errorEl.hidden = false;
        stopLoading();
    }

    // Free trial, 3DS branch: confirm the SetupIntent, then ask the
    // server to create the trialing subscription. Two steps because the
    // card has to be proven before a subscription is created against
    // it, not after.
    function confirmTrialSetup(data, paymentMethodId) {
        return stripe.confirmCardSetup(data.client_secret).then(
            function (result) {
                if (result.error) {
                    showError(result.error.message);
                    return;
                }
                return fetch(data.finalize_url, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrf(),
                    },
                    body: JSON.stringify({
                        acceptance_ref: data.acceptance_ref,
                        payment_method_id: paymentMethodId,
                    }),
                }).then(function (r) { return r.json(); })
                  .then(function (done) {
                      if (done.error) {
                          showError(done.error);
                          return;
                      }
                      if (done.ok && done.redirect) {
                          window.location.href = done.redirect;
                      } else {
                          // Card proved but the trial did not start.
                          // Say so rather than show a success page: the
                          // buyer needs to know nothing is running.
                          showError('Your card was confirmed but we could '
                              + 'not start your trial. Nothing has been '
                              + 'charged. Please email '
                              + 'zacherylong@aspiredwebsites.com.');
                      }
                  });
            });
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        errorEl.hidden = true;
        startLoading();

        var email = emailEl.value.trim();
        if (!email) {
            showError('Please enter your email.');
            return;
        }
        if (termsEl && !termsEl.checked) {
            showError('Please accept the Terms of Service to continue.');
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
                    terms_accepted: termsEl ? termsEl.checked : false,
                }),
            }).then(function (r) { return r.json(); })
              .then(function (data) {
                  if (data.error) {
                      showError(data.error);
                      return;
                  }
                  if (data.requires_action && data.setup_intent) {
                      // Free-trial 3DS. The subscription does not exist
                      // yet: confirming the SetupIntent only proves the
                      // card, so we have to call back to finalize/ to
                      // actually start the trial. Navigating straight to
                      // the success page here would show someone a
                      // "your trial started" page with no trial behind
                      // it.
                      return confirmTrialSetup(data,
                                               pmResult.paymentMethod.id);
                  }
                  if (data.requires_action) {
                      // Charge-now 3DS, unchanged: the PaymentIntent is
                      // already attached to a created subscription.
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
