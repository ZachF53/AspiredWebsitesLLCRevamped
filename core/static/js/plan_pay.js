// plan_pay.js — Stripe Elements card collection for the plan pay page
// (billing/templates/billing/pay_plan.html). Mirrors checkout.js's
// payment_method_id + server-side confirm flow (see
// billing.plan_billing.complete_awaiting_plan_payment): the browser
// only ever creates a PaymentMethod, never confirms a charge directly
// — the server attaches it, creates the subscription, and confirms
// the first invoice's PaymentIntent, handing back requires_action +
// client_secret if the card needs SCA/3DS.
//
// CSP requirements: js.stripe.com must be allowed in script-src (see
// core/middleware.py CSP_PAYMENT, gated on the /plan-pay/ path prefix).
(function init() {
    'use strict';

    var form = document.getElementById('plan-pay-form');
    if (!form || typeof window.Stripe !== 'function') {
        if (form && typeof window.Stripe !== 'function') {
            setTimeout(init, 100);
        }
        return;
    }

    var configEl = document.getElementById('plan-pay-config');
    if (!configEl) return;
    var config = JSON.parse(configEl.textContent);
    if (!config.publishable_key) return;

    var stripe = Stripe(config.publishable_key);

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
        },
    };

    var elements = stripe.elements({
        mode: 'setup',
        currency: 'usd',
        paymentMethodCreation: 'manual',
        appearance: appearance,
    });
    var paymentElement = elements.create('payment');
    paymentElement.mount('#plan-pay-element');

    var submitBtn = document.getElementById('plan-pay-submit');
    var buttonText = document.getElementById('plan-pay-button-text');
    var spinner = document.getElementById('plan-pay-spinner');
    var messageEl = document.getElementById('plan-pay-message');

    function getCsrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }

    function startLoading() {
        submitBtn.disabled = true;
        spinner.hidden = false;
        buttonText.textContent = 'Processing…';
    }
    function stopLoading() {
        submitBtn.disabled = false;
        spinner.hidden = true;
        buttonText.textContent = 'Add card & activate';
    }
    function showMessage(msg) {
        messageEl.textContent = msg || 'Something went wrong. Please try again.';
        messageEl.hidden = false;
        stopLoading();
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        messageEl.hidden = true;
        startLoading();

        elements.submit().then(function (result) {
            if (result.error) {
                showMessage(result.error.message);
                return;
            }
            return stripe.createPaymentMethod({ elements: elements });
        }).then(function (pmResult) {
            if (!pmResult) return; // already errored
            if (pmResult.error) {
                showMessage(pmResult.error.message);
                return;
            }
            return fetch(config.confirm_url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrf(),
                },
                body: JSON.stringify({
                    payment_method_id: pmResult.paymentMethod.id,
                }),
            }).then(function (r) { return r.json(); })
              .then(function (data) {
                  if (data.error) {
                      showMessage(data.error);
                      return;
                  }
                  if (data.requires_action) {
                      return stripe.confirmCardPayment(data.client_secret).then(
                          function (result) {
                              if (result.error) {
                                  showMessage(result.error.message);
                                  return;
                              }
                              window.location.href = config.success_url;
                          });
                  }
                  if (data.ok) {
                      window.location.href = config.success_url;
                  }
              });
        }).catch(function (err) {
            showMessage((err && err.message) || 'Network error');
        });
    });
})();
