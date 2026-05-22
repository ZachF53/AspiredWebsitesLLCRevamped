/* Deployment dashboard — live-fill command blocks, copy buttons, toggles.
   CSP-safe: external file, no inline handlers, no eval. */
(function () {
    'use strict';

    // Token name -> the input element id that feeds it.
    var TOKEN_INPUTS = {
        SERVER_IP: 'input-ip',
        DOMAIN: 'input-domain',
        CLIENT_NAME: 'input-client',
        GITHUB_REPO: 'input-repo',
        SUPERUSER_PASSWORD: 'input-password'
    };
    var TOKENS = Object.keys(TOKEN_INPUTS);

    function escapeHtml(s) {
        return s.replace(/[&<>"']/g, function (c) {
            return {
                '&': '&amp;', '<': '&lt;', '>': '&gt;',
                '"': '&quot;', "'": '&#39;'
            }[c];
        });
    }

    // Cache each command block's raw template (with {{TOKENS}} intact).
    var templates = {};
    document.querySelectorAll('.cmd-src').forEach(function (src) {
        templates[src.getAttribute('data-cmd')] = src.textContent.trim();
    });

    function currentValues() {
        var vals = {};
        TOKENS.forEach(function (tok) {
            var el = document.getElementById(TOKEN_INPUTS[tok]);
            vals[tok] = el ? el.value.trim() : '';
        });
        return vals;
    }

    function lineHtml(line, vals) {
        var isComment = /^\s*#/.test(line);
        var html = escapeHtml(line);
        TOKENS.forEach(function (tok) {
            var token = '{{' + tok + '}}';
            var val = vals[tok];
            var rep = val
                ? '<span class="cmd-val">' + escapeHtml(val) + '</span>'
                : '<span class="cmd-token">' + token + '</span>';
            html = html.split(token).join(rep);
        });
        return '<span class="cmd-line' + (isComment ? ' cmd-line--comment' : '') +
               '">' + (html || ' ') + '</span>';
    }

    function render() {
        var vals = currentValues();
        document.querySelectorAll('.cmd-out').forEach(function (out) {
            var tpl = templates[out.getAttribute('data-cmd')];
            if (tpl == null) {
                return;
            }
            out.innerHTML = tpl.split('\n').map(function (l) {
                return lineHtml(l, vals);
            }).join('\n');
        });
    }

    function flash(btn, message) {
        btn.textContent = message;
        btn.classList.add('is-copied');
        setTimeout(function () {
            btn.textContent = 'Copy';
            btn.classList.remove('is-copied');
        }, 2000);
    }

    // ── Wire up ──────────────────────────────────────────────────────────
    TOKENS.forEach(function (tok) {
        var el = document.getElementById(TOKEN_INPUTS[tok]);
        if (el) {
            el.addEventListener('input', render);
        }
    });

    document.querySelectorAll('.cmd-copy').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var block = btn.closest('.command-block');
            var out = block ? block.querySelector('.cmd-out') : null;
            if (!out) {
                return;
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(out.textContent).then(
                    function () { flash(btn, 'Copied ✓'); },
                    function () { flash(btn, 'Copy failed'); }
                );
            } else {
                flash(btn, 'Copy unavailable');
            }
        });
    });

    var pwToggle = document.querySelector('.pw-toggle');
    if (pwToggle) {
        pwToggle.addEventListener('click', function () {
            var inp = document.getElementById('input-password');
            if (!inp) {
                return;
            }
            if (inp.type === 'password') {
                inp.type = 'text';
                pwToggle.textContent = 'Hide';
            } else {
                inp.type = 'password';
                pwToggle.textContent = 'Show';
            }
        });
    }

    // Fresh-deploy: pre-fill inputs from a selected client.
    var prefillBtn = document.getElementById('prefill-btn');
    if (prefillBtn) {
        prefillBtn.addEventListener('click', function () {
            var sel = document.getElementById('client-prefill');
            if (!sel || !sel.value) {
                return;
            }
            var opt = sel.options[sel.selectedIndex];
            setInput('input-ip', opt.getAttribute('data-ip'));
            setInput('input-domain', opt.getAttribute('data-domain'));
            setInput('input-client', opt.getAttribute('data-name'));
            render();
        });
    }

    // Home: jump to a client's deploy page.
    var goBtn = document.getElementById('client-deploy-go');
    if (goBtn) {
        goBtn.addEventListener('click', function () {
            var sel = document.getElementById('client-deploy-picker');
            if (sel && sel.value) {
                window.location.href = sel.value;
            }
        });
    }

    function setInput(id, val) {
        var el = document.getElementById(id);
        if (el) {
            el.value = val || '';
        }
    }

    render();
})();
