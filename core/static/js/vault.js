/* Vault — PIN digit entry, countdowns, credential reveal, copy, toggles.
   CSP-safe: external file, no inline handlers, no eval. */
(function () {
    'use strict';

    // ── Password show/hide toggles ───────────────────────────────────────
    document.querySelectorAll('.vault-pw-toggle').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var input = document.getElementById(btn.getAttribute('data-target'));
            if (!input) { return; }
            if (input.type === 'password') {
                input.type = 'text';
                btn.textContent = 'Hide';
            } else {
                input.type = 'password';
                btn.textContent = 'Show';
            }
        });
    });

    // ── PIN digit boxes — auto-advance + auto-submit ─────────────────────
    var digits = document.querySelectorAll('.vault-digit');
    if (digits.length) {
        digits.forEach(function (box, i) {
            box.addEventListener('input', function () {
                box.value = box.value.replace(/[^0-9]/g, '').slice(0, 1);
                if (box.value && i < digits.length - 1) {
                    digits[i + 1].focus();
                }
                var filled = Array.prototype.every.call(digits, function (d) {
                    return d.value.length === 1;
                });
                if (filled) {
                    var form = document.getElementById('vault-pin-form');
                    if (form) { form.submit(); }
                }
            });
            box.addEventListener('keydown', function (e) {
                if (e.key === 'Backspace' && !box.value && i > 0) {
                    digits[i - 1].focus();
                }
            });
        });
    }

    // ── Lockout countdown ────────────────────────────────────────────────
    var lockEl = document.getElementById('vault-lockout-countdown');
    if (lockEl) {
        var until = new Date(lockEl.getAttribute('data-until')).getTime();
        var tickLock = function () {
            var left = Math.floor((until - Date.now()) / 1000);
            if (left <= 0) {
                window.location.reload();
                return;
            }
            var m = Math.floor(left / 60), s = left % 60;
            lockEl.textContent = m + ':' + (s < 10 ? '0' : '') + s;
        };
        tickLock();
        setInterval(tickLock, 1000);
    }

    // ── Session countdown + expiry overlay ───────────────────────────────
    var wrap = document.querySelector('.admin-vault[data-seconds-remaining]');
    var sessionEl = document.getElementById('vault-session-countdown');
    var overlay = document.getElementById('vault-expired-overlay');
    if (wrap && sessionEl) {
        var remaining = parseInt(wrap.getAttribute('data-seconds-remaining'), 10) || 0;
        var tickSession = function () {
            if (remaining <= 0) {
                sessionEl.textContent = '0:00';
                if (overlay) { overlay.hidden = false; }
                return;
            }
            var m = Math.floor(remaining / 60), s = remaining % 60;
            sessionEl.textContent = m + ':' + (s < 10 ? '0' : '') + s;
            remaining -= 1;
            setTimeout(tickSession, 1000);
        };
        tickSession();
    }
    var reunlock = document.getElementById('vault-reunlock');
    if (reunlock) {
        reunlock.addEventListener('click', function () { window.location.reload(); });
    }

    // ── Delete confirmation ──────────────────────────────────────────────
    document.querySelectorAll('.vault-delete-btn').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            if (!window.confirm('Delete this credential permanently?')) {
                e.preventDefault();
            }
        });
    });

    // ── Credential form: template quick-fill ─────────────────────────────
    document.querySelectorAll('.vault-tpl-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var label = document.getElementById('id_label');
            var category = document.getElementById('id_category');
            if (label) { label.value = btn.getAttribute('data-label'); }
            if (category) { category.value = btn.getAttribute('data-category'); }
        });
    });

    // ── Credential form (edit): disable sensitive inputs until "Replace" ─
    document.querySelectorAll('.vault-change-flag input[type="checkbox"]').forEach(
        function (cb) {
            var name = cb.getAttribute('name') || '';
            if (name.indexOf('change_') !== 0) { return; }
            var field = name.slice(7);
            var input = document.querySelector('[name="' + field + '"]');
            if (!input) { return; }
            input.disabled = !cb.checked;
            cb.addEventListener('change', function () {
                input.disabled = !cb.checked;
                if (cb.checked) { input.focus(); }
            });
        }
    );

    // ── Credential form: SSH section + auth-type toggles ─────────────────
    var sshCheckbox = document.getElementById('id_is_ssh_credential');
    var sshFields = document.getElementById('ssh-fields');
    if (sshCheckbox && sshFields) {
        var syncSshVisible = function () {
            sshFields.classList.toggle('is-visible', sshCheckbox.checked);
        };
        sshCheckbox.addEventListener('change', syncSshVisible);
        syncSshVisible();

        var syncAuthType = function () {
            var checked = document.querySelector(
                'input[name="ssh_auth_type"]:checked');
            var isKey = !!checked && checked.value === 'private_key';
            document.querySelectorAll('.ssh-auth-password').forEach(function (el) {
                el.classList.toggle('is-hidden', isKey);
            });
            document.querySelectorAll('.ssh-auth-key').forEach(function (el) {
                el.classList.toggle('is-hidden', !isKey);
            });
        };
        document.querySelectorAll('input[name="ssh_auth_type"]').forEach(
            function (radio) {
                radio.addEventListener('change', syncAuthType);
            });
        syncAuthType();
    }

    // ── Credential reveal ────────────────────────────────────────────────
    var csrf = '';
    var csrfWrap = document.querySelector('.admin-vault[data-csrf]');
    if (csrfWrap) { csrf = csrfWrap.getAttribute('data-csrf'); }

    function makeCopyBtn(getText) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'vault-copy-btn';
        b.textContent = 'Copy';
        b.addEventListener('click', function () {
            if (navigator.clipboard) {
                navigator.clipboard.writeText(getText()).then(function () {
                    b.textContent = 'Copied';
                    setTimeout(function () { b.textContent = 'Copy'; }, 1500);
                });
            }
        });
        return b;
    }

    document.querySelectorAll('.vault-cred').forEach(function (card) {
        var btn = card.querySelector('.vault-reveal');
        if (!btn) { return; }
        var url = card.getAttribute('data-reveal-url');
        var bar = card.querySelector('.vault-reveal-bar');
        var fields = {
            username: card.querySelector('[data-field="username"]'),
            password: card.querySelector('[data-field="password"]')
        };
        var originals = {};
        var hideTimer = null;

        function hide() {
            Object.keys(fields).forEach(function (k) {
                if (fields[k] && originals[k] !== undefined) {
                    fields[k].textContent = originals[k];
                }
            });
            card.querySelectorAll('.vault-copy-btn').forEach(function (c) {
                c.remove();
            });
            if (bar) { bar.hidden = true; bar.classList.remove('is-counting'); }
            btn.textContent = 'Reveal';
            btn.disabled = false;
        }

        btn.addEventListener('click', function () {
            if (btn.textContent === 'Hide') { clearTimeout(hideTimer); hide(); return; }
            btn.disabled = true;
            btn.textContent = '…';
            fetch(url, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest' }
            }).then(function (r) {
                if (!r.ok) { throw new Error('locked'); }
                return r.json();
            }).then(function (data) {
                ['username', 'password'].forEach(function (k) {
                    var el = fields[k];
                    if (!el) { return; }
                    originals[k] = el.textContent;
                    el.textContent = data[k] || '—';
                    if (data[k]) {
                        el.parentNode.appendChild(makeCopyBtn(function () {
                            return data[k];
                        }));
                    }
                });
                btn.textContent = 'Hide';
                btn.disabled = false;
                if (bar) {
                    bar.hidden = false;
                    // restart the 30s CSS countdown animation
                    bar.classList.remove('is-counting');
                    void bar.offsetWidth;
                    bar.classList.add('is-counting');
                }
                hideTimer = setTimeout(hide, 30000);
            }).catch(function () {
                btn.textContent = 'Vault locked — reload';
                btn.disabled = false;
            });
        });
    });
})();
