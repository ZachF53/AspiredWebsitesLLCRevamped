/* Browser SSH terminal — xterm.js bridged to a Channels WebSocket.
   CSP-safe: external file, no inline handlers. */
(function () {
    'use strict';

    var wrap = document.querySelector('.term-wrap');
    if (!wrap || typeof Terminal === 'undefined') { return; }

    var credId = wrap.getAttribute('data-cred-id');
    var vaultUrl = wrap.getAttribute('data-vault-url') || '/admin-dashboard/vault/';
    var remaining = parseInt(wrap.getAttribute('data-totp-remaining'), 10) || 0;

    var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var wsUrl = wsProtocol + '//' + window.location.host + '/ws/ssh/' + credId + '/';

    // ── xterm ────────────────────────────────────────────────────────────
    var term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: 'Consolas, "Courier New", monospace',
        theme: {
            background: '#070614',
            foreground: '#e8e8e8',
            cursor: '#E8650A',
        }
    });
    var fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('terminal'));
    try { fitAddon.fit(); } catch (e) { /* ignore */ }

    function sendResize() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'resize', cols: term.cols, rows: term.rows
            }));
        }
    }

    window.addEventListener('resize', function () {
        try { fitAddon.fit(); } catch (e) { /* ignore */ }
        sendResize();
    });

    // ── WebSocket ────────────────────────────────────────────────────────
    var ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        term.write('\r\nConnecting...\r\n');
        sendResize();
    };
    ws.onmessage = function (event) {
        var msg;
        try { msg = JSON.parse(event.data); } catch (e) { return; }
        if (msg.type === 'output') {
            term.write(msg.data);
        } else if (msg.type === 'error') {
            term.write('\r\n\x1b[31m' + msg.message + '\x1b[0m\r\n');
        }
    };
    ws.onclose = function () {
        term.write('\r\n\x1b[33mConnection closed.\x1b[0m\r\n');
    };
    ws.onerror = function () {
        term.write('\r\n\x1b[31mConnection error.\x1b[0m\r\n');
    };

    term.onData(function (data) {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'input', data: data }));
        }
    });

    // ── Session countdown ────────────────────────────────────────────────
    var timerEl = document.getElementById('session-timer');
    var countdown = setInterval(function () {
        remaining -= 1;
        if (remaining <= 0) {
            clearInterval(countdown);
            timerEl.textContent = '00:00';
            try { ws.close(); } catch (e) { /* ignore */ }
            term.write('\r\n\x1b[31mSession expired. Reconnect to continue.\x1b[0m\r\n');
            return;
        }
        var m = Math.floor(remaining / 60), s = remaining % 60;
        timerEl.textContent =
            String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
        if (remaining <= 180) { timerEl.classList.add('term-timer--low'); }
    }, 1000);

    // ── Command library ──────────────────────────────────────────────────
    document.querySelectorAll('.term-cmd__run').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var command = btn.getAttribute('data-command') || '';
            var needsConfirm = btn.getAttribute('data-confirm') === '1';
            var dangerous = btn.getAttribute('data-dangerous') === '1';
            if (needsConfirm &&
                !window.confirm('Run this command?\n\n' + command)) {
                return;
            }
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'command',
                    data: command + '\n',
                    dangerous: dangerous
                }));
            }
            term.focus();
        });
    });

    // ── Top-bar buttons ──────────────────────────────────────────────────
    document.getElementById('disconnect-btn').addEventListener('click', function () {
        try { ws.close(); } catch (e) { /* ignore */ }
        window.location.href = vaultUrl;
    });

    document.getElementById('cmd-library-btn').addEventListener('click', function () {
        document.getElementById('cmd-sidebar').classList.toggle('is-hidden');
        try { fitAddon.fit(); } catch (e) { /* ignore */ }
        sendResize();
    });

    term.focus();
})();
