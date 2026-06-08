// add_card.js — SetupIntent flow for adding a new card on file.
(function () {
    'use strict';

    var form = document.getElementById('add-card-form');
    if (!form || typeof window.Stripe !== 'function') {
        if (form && typeof window.Stripe !== 'function') {
            setTimeout(arguments.callee, 100);
        }
        return;
    }

    var clientSecret = form.getAttribute('data-client-secret');
    var pubKey = form.getAttribute('data-publishable-key');
    var returnUrl = form.getAttribute('data-return-url');
    var errorEl = document.getElementById('add-card-error');

    var stripe = Stripe(pubKey);
    var elements = stripe.elements({ clientSecret: clientSecret });
    var paymentElement = elements.create('payment');
    paymentElement.mount('#payment-element');

    function showError(msg) {
        errorEl.textContent = msg;
        errorEl.hidden = false;
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        errorEl.hidden = true;
        stripe.confirmSetup({
            elements: elements,
            confirmParams: {
                return_url: window.location.origin + returnUrl,
            },
        }).then(function (result) {
            if (result.error) showError(result.error.message);
        });
    });
})();
