// onboarding_wizard.js — auto-save + skip handling for the wizard step
// page. CSP-safe (external file, no eval, no inline handlers).
//
// On each input's blur OR change event, POST {question_key, value}
// to the page's save URL. On skip-button click, POST {question_key}
// to the skip URL and toggle the disabled state of the input.
// Both endpoints return the latest progress so we can update the bar.
(function () {
    'use strict';

    var card = document.querySelector('[data-onboarding-save-url]');
    if (!card) return;

    var saveUrl = card.getAttribute('data-onboarding-save-url');
    var skipUrl = card.getAttribute('data-onboarding-skip-url');

    function csrfToken() {
        // CSRF_COOKIE_HTTPONLY is True on this project, so the cookie is
        // invisible to JS — read the token rendered by {% csrf_token %}
        // first, then fall back to the cookie if it's ever readable.
        var input = document.querySelector(
            'input[name=csrfmiddlewaretoken]');
        if (input && input.value) return input.value;
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : '';
    }

    function postJson(url, payload) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken(),
            },
            body: JSON.stringify(payload),
        }).then(function (r) {
            if (!r.ok) throw new Error('save failed: ' + r.status);
            return r.json();
        });
    }

    function updateProgress(progress) {
        if (!progress) return;
        var fill = document.getElementById('ob-progress-fill');
        var text = document.getElementById('ob-progress-text');
        if (fill) fill.style.width = progress.percent + '%';
        if (text) text.textContent = progress.percent + '%';
        var caption = document.querySelector('.ob-progress__caption');
        if (caption) {
            // Caption tail after the % span
            caption.innerHTML =
                '<span id="ob-progress-text">' + progress.percent + '%</span>' +
                ' (' + progress.answered + ' answered, ' +
                progress.skipped + ' skipped, ' + progress.total + ' total)';
        }
    }

    function flashSaved(key) {
        var el = card.querySelector('[data-q-saved="' + key + '"]');
        if (!el) return;
        el.hidden = false;
        setTimeout(function () { el.hidden = true; }, 1200);
    }

    function bindInput(input) {
        var key = input.getAttribute('data-q-input');
        if (!key) return;
        function save() {
            if (input.disabled) return;
            postJson(saveUrl, {
                question_key: key,
                value: input.value,
            }).then(function (data) {
                flashSaved(key);
                updateProgress(data.progress);
            }).catch(function () {
                // Silent — leave the value in the field; user can blur
                // again or hit save & continue to retry.
            });
        }
        // Save on 'change' only — for text/textarea it fires when the
        // field loses focus AND the value actually changed; for selects
        // it fires on pick. That's exactly "save once they click out,
        // and only if they edited it" (no save / no "✓ saved" on an
        // untouched field).
        input.addEventListener('change', save);
    }

    function bindSkip(btn) {
        var key = btn.getAttribute('data-q-skip');
        if (!key) return;
        btn.addEventListener('click', function () {
            var input = card.querySelector('[data-q-input="' + key + '"]');
            var pressed = btn.getAttribute('aria-pressed') === 'true';
            if (pressed) {
                // Un-skip — just save the current value (which is empty)
                postJson(saveUrl, {
                    question_key: key,
                    value: input ? input.value : '',
                }).then(function (data) {
                    if (input) input.disabled = false;
                    btn.setAttribute('aria-pressed', 'false');
                    btn.textContent = 'Skip this question';
                    updateProgress(data.progress);
                });
            } else {
                postJson(skipUrl, { question_key: key }).then(function (data) {
                    if (input) input.disabled = true;
                    btn.setAttribute('aria-pressed', 'true');
                    btn.textContent = '↩ Un-skip';
                    updateProgress(data.progress);
                });
            }
        });
    }

    card.querySelectorAll('[data-q-input]').forEach(bindInput);
    card.querySelectorAll('[data-q-skip]').forEach(bindSkip);
})();
