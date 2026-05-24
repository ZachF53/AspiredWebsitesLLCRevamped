/*
 * Portal subscriptions page — "Add card" modal driven by Stripe
 * SetupIntent + the Payment Element.
 *
 * Flow:
 *   1. User clicks "+ Add card"
 *   2. JS POSTs to /portal/subscriptions/payment-methods/add/ with CSRF
 *   3. Server returns { client_secret: "seti_..." }
 *   4. JS opens modal, mounts Payment Element keyed to that SetupIntent
 *   5. User fills card → submit → stripe.confirmSetup() → on success,
 *      reload the page so the new card shows up in the list
 *
 * Strict-CSP-safe: no inline handlers, no eval. Stripe.js loaded
 * separately via <script src="https://js.stripe.com/v3/" defer> in
 * the template.
 */
(function () {
    'use strict';

    function csrfToken() {
        var el = document.querySelector(
            'input[name="csrfmiddlewaretoken"]');
        return el ? el.value : '';
    }

    function readPublishableKey() {
        var raw = document.getElementById('stripe-publishable-key');
        if (!raw) { return ''; }
        try { return JSON.parse(raw.textContent); }
        catch (e) { return ''; }
    }

    function showMessage(text, kind) {
        var el = document.getElementById('add-card-message');
        if (!el) { return; }
        el.textContent = text;
        el.className = 'add-card-modal__message add-card-modal__message--'
            + (kind || 'error');
        el.hidden = false;
    }

    function setLoading(isLoading) {
        var btn = document.getElementById('add-card-submit');
        var text = document.getElementById('add-card-text');
        var spin = document.getElementById('add-card-spinner');
        if (btn) { btn.disabled = isLoading; }
        if (text) { text.hidden = isLoading; }
        if (spin) { spin.hidden = !isLoading; }
    }

    function openModal() {
        var modal = document.getElementById('add-card-modal');
        if (modal) { modal.hidden = false; }
    }
    function closeModal() {
        var modal = document.getElementById('add-card-modal');
        if (modal) { modal.hidden = true; }
    }

    var stripe = null;
    var elements = null;

    function startAddCard() {
        var pk = readPublishableKey();
        if (!pk) {
            alert('Stripe is not configured on this account.');
            return;
        }
        if (typeof Stripe !== 'function') {
            // Stripe.js hasn't loaded yet — wait a tick.
            return setTimeout(startAddCard, 200);
        }

        setLoading(true);
        openModal();

        fetch('/portal/subscriptions/payment-methods/add/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': csrfToken() },
        }).then(function (r) { return r.json(); })
          .then(function (data) {
              if (data.error) {
                  showMessage(data.error, 'error');
                  setLoading(false);
                  return;
              }
              stripe = Stripe(pk);
              elements = stripe.elements({
                  clientSecret: data.client_secret,
                  appearance: {
                      theme: 'night',
                      variables: {
                          colorPrimary: '#E8650A',
                          colorBackground: '#070614',
                          colorText: '#F8FAFC',
                          colorDanger: '#EF4444',
                          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif',
                          borderRadius: '8px',
                      },
                  },
              });
              var pe = elements.create('payment', { layout: 'tabs' });
              pe.mount('#add-card-element');
              setLoading(false);
          })
          .catch(function (err) {
              showMessage('Could not start card setup: '
                  + (err.message || err), 'error');
              setLoading(false);
          });
    }

    function submitCard(e) {
        e.preventDefault();
        if (!stripe || !elements) { return; }
        setLoading(true);

        stripe.confirmSetup({
            elements: elements,
            confirmParams: {
                // After success Stripe redirects back here, but if we
                // tell it `redirect: 'if_required'` it only redirects
                // for 3DS flows. Most cards succeed in-page.
                return_url: window.location.href,
            },
            redirect: 'if_required',
        }).then(function (result) {
            if (result.error) {
                showMessage(result.error.message
                    || 'Could not save card.', 'error');
                setLoading(false);
                return;
            }
            // Success — reload so the new card shows up in the list.
            window.location.reload();
        }).catch(function (err) {
            showMessage('Unexpected error: '
                + (err.message || err), 'error');
            setLoading(false);
        });
    }

    function init() {
        var openBtn = document.getElementById('add-card-btn');
        var form = document.getElementById('add-card-form');
        var closers = document.querySelectorAll('[data-add-card-close]');

        if (openBtn) { openBtn.addEventListener('click', startAddCard); }
        if (form) { form.addEventListener('submit', submitCard); }
        closers.forEach(function (c) {
            c.addEventListener('click', closeModal);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
