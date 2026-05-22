/*
 * Aspired Websites — AI chatbot widget.
 *
 * Embed on a client site with:
 *   <script src="https://aspiredwebsites.com/static/js/aspired-chat.js"
 *           data-aspired-client="CLIENT-UUID" defer></script>
 */
(function () {
    'use strict';

    var BASE = 'https://aspiredwebsites.com';
    var CHAT_URL = BASE + '/api/chat/';
    var CONFIG_URL = BASE + '/api/chat/config/';

    var tag = document.currentScript ||
        document.querySelector('script[data-aspired-client]');
    var CLIENT_ID = tag ? tag.getAttribute('data-aspired-client') : '';
    if (!CLIENT_ID) { return; }

    var sessionId = sessionStorage.getItem('aspired-chat-session');
    if (!sessionId) {
        sessionId = 's-' + Date.now() + '-' +
            Math.random().toString(36).slice(2);
        sessionStorage.setItem('aspired-chat-session', sessionId);
    }

    var history = [];
    var sending = false;

    fetch(CONFIG_URL + encodeURIComponent(CLIENT_ID) + '/')
        .then(function (r) { return r.json(); })
        .then(function (cfg) { if (cfg && cfg.active) { build(cfg); } })
        .catch(function () { /* widget simply does not appear */ });

    function build(cfg) {
        var color = cfg.color || '#E8650A';
        var side = cfg.position === 'bottom-left' ? 'left' : 'right';

        var style = document.createElement('style');
        style.textContent = [
            '.acw-btn{position:fixed;bottom:20px;' + side + ':20px;width:58px;',
            'height:58px;border-radius:50%;background:' + color + ';color:#fff;',
            'border:none;cursor:pointer;font-size:26px;z-index:2147483000;',
            'box-shadow:0 4px 16px rgba(0,0,0,.28);}',
            '.acw-panel{position:fixed;bottom:88px;' + side + ':20px;width:330px;',
            'max-width:calc(100vw - 40px);height:460px;max-height:calc(100vh - 120px);',
            'background:#fff;border-radius:14px;display:none;flex-direction:column;',
            'overflow:hidden;z-index:2147483000;box-shadow:0 8px 32px rgba(0,0,0,.3);',
            'font-family:Arial,Helvetica,sans-serif;}',
            '.acw-panel.acw-open{display:flex;}',
            '.acw-head{background:' + color + ';color:#fff;padding:14px 16px;',
            'font-weight:bold;font-size:15px;}',
            '.acw-msgs{flex:1;overflow-y:auto;padding:14px;background:#f5f5f5;}',
            '.acw-msg{margin-bottom:10px;display:flex;}',
            '.acw-msg.acw-user{justify-content:flex-end;}',
            '.acw-bubble{max-width:80%;padding:9px 12px;border-radius:12px;',
            'font-size:14px;line-height:1.45;white-space:pre-wrap;}',
            '.acw-bot .acw-bubble{background:#fff;color:#1a1a1a;',
            'border:1px solid #e2e2e2;}',
            '.acw-user .acw-bubble{background:' + color + ';color:#fff;}',
            '.acw-foot{display:flex;border-top:1px solid #e2e2e2;background:#fff;}',
            '.acw-input{flex:1;border:none;padding:12px;font-size:14px;',
            'font-family:inherit;outline:none;}',
            '.acw-send{border:none;background:' + color + ';color:#fff;',
            'padding:0 16px;cursor:pointer;font-weight:bold;}'
        ].join('');
        document.head.appendChild(style);

        var btn = document.createElement('button');
        btn.className = 'acw-btn';
        btn.setAttribute('aria-label', 'Open chat');
        btn.textContent = '💬';

        var panel = document.createElement('div');
        panel.className = 'acw-panel';
        panel.innerHTML =
            '<div class="acw-head">Chat with us</div>' +
            '<div class="acw-msgs"></div>' +
            '<div class="acw-foot">' +
            '<input class="acw-input" type="text" placeholder="Type a message…">' +
            '<button class="acw-send" type="button">Send</button></div>';

        document.body.appendChild(btn);
        document.body.appendChild(panel);

        var msgs = panel.querySelector('.acw-msgs');
        var input = panel.querySelector('.acw-input');
        var sendBtn = panel.querySelector('.acw-send');
        var greeted = false;

        function addMessage(role, text) {
            var wrap = document.createElement('div');
            wrap.className = 'acw-msg ' + (role === 'user' ? 'acw-user' : 'acw-bot');
            var bubble = document.createElement('div');
            bubble.className = 'acw-bubble';
            bubble.textContent = text;
            wrap.appendChild(bubble);
            msgs.appendChild(wrap);
            msgs.scrollTop = msgs.scrollHeight;
            return bubble;
        }

        btn.addEventListener('click', function () {
            panel.classList.toggle('acw-open');
            if (panel.classList.contains('acw-open')) {
                if (!greeted) {
                    addMessage('assistant', cfg.greeting ||
                        'Hi! How can I help you?');
                    greeted = true;
                }
                input.focus();
            }
        });

        function send() {
            var text = input.value.trim();
            if (!text || sending) { return; }
            input.value = '';
            addMessage('user', text);
            history.push({ role: 'user', content: text });
            sending = true;
            var typing = addMessage('assistant', '…');

            fetch(CHAT_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain' },
                body: JSON.stringify({
                    client_id: CLIENT_ID,
                    session_id: sessionId,
                    message: text,
                    conversation_history: history
                })
            }).then(function (r) {
                return r.json();
            }).then(function (data) {
                var reply = (data && data.response) ||
                    'Sorry — something went wrong. Please try again.';
                typing.textContent = reply;
                history.push({ role: 'assistant', content: reply });
            }).catch(function () {
                typing.textContent =
                    'Sorry — I could not reach the server. Please try again.';
            }).then(function () {
                sending = false;
                msgs.scrollTop = msgs.scrollHeight;
            });
        }

        sendBtn.addEventListener('click', send);
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); send(); }
        });
    }
})();
