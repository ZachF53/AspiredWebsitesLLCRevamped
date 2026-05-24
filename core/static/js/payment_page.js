/*
 * Stripe Payment Element wiring for the public /pay/<token>/ page.
 *
 * Config comes from #stripe-config (json_script in the template) and
 * carries the publishable_key, the PaymentIntent's client_secret, and
 * the success_url to redirect to on confirmation.
 *
 * Strict-CSP-safe: no inline handlers. Loaded by the template with
 * `defer` after https://js.stripe.com/v3/.
 */
(function () {
    'use strict';

    function readConfig() {
        var raw = document.getElementById('stripe-config');
        if (!raw) { return null; }
        try {
            return JSON.parse(raw.textContent);
        } catch (e) {
            return null;
        }
    }

    function showMessage(text, kind) {
        var el = document.getElementById('payment-message');
        if (!el) { return; }
        el.textContent = text;
        el.className = 'pay-message pay-message--' + (kind || 'error');
        el.hidden = false;
    }

    function setLoading(isLoading) {
        var btn = document.getElementById('submit');
        var text = document.getElementById('button-text');
        var spinner = document.getElementById('spinner');
        if (btn) { btn.disabled = isLoading; }
        if (text) { text.hidden = isLoading; }
        if (spinner) { spinner.hidden = !isLoading; }
    }

    function init() {
        var cfg = readConfig();
        if (!cfg || !cfg.publishable_key || !cfg.client_secret) {
            showMessage('Payment is not configured. Please contact us.', 'error');
            return;
        }

        // Wait for Stripe.js to load (the <script> has `defer`).
        if (typeof Stripe !== 'function') {
            return setTimeout(init, 100);
        }

        var stripe = Stripe(cfg.publishable_key);
        var elements = stripe.elements({
            clientSecret: cfg.client_secret,
            // Dark theme to match the rest of the site. Stripe Element
            // colors picked to match our --color-bg-raised etc.
            appearance: {
                theme: 'night',
                variables: {
                    colorPrimary: '#E8650A',
                    colorBackground: '#070614',
                    colorText: '#F8FAFC',
                    colorDanger: '#EF4444',
                    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif',
                    borderRadius: '8px',
                    spacingUnit: '4px',
                },
                rules: {
                    '.Input': {
                        border: '1px solid #1E293B',
                        backgroundColor: '#0F172A',
                    },
                    '.Input:focus': {
                        borderColor: '#E8650A',
                        boxShadow: '0 0 0 1px #E8650A',
                    },
                    '.Label': {
                        color: '#94A3B8',
                        fontWeight: '500',
                    },
                },
            },
        });

        // Payment Element — card only (the PaymentIntent was created
        // with payment_method_types=['card'], so Stripe only renders
        // the card form — no Apple Pay / Google Pay / Link).
        var paymentElement = elements.create('payment', {
            layout: 'tabs',
        });
        paymentElement.mount('#payment-element');

        var form = document.getElementById('payment-form');
        if (!form) { return; }

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            setLoading(true);

            stripe.confirmPayment({
                elements: elements,
                confirmParams: {
                    return_url: cfg.success_url,
                },
            }).then(function (result) {
                // If confirmation succeeds without a redirect-required
                // flow, Stripe still sends the browser to return_url.
                // We only reach .then(...).error here on declines /
                // validation failures — the success case fires the
                // redirect first.
                if (result.error) {
                    var msg = result.error.message || 'Payment failed. Please try again.';
                    showMessage(msg, 'error');
                    setLoading(false);
                }
            }).catch(function (err) {
                showMessage(
                    'Something went wrong: ' + (err.message || err),
                    'error');
                setLoading(false);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
