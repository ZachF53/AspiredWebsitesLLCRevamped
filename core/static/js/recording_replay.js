/* Boots the rrweb player on the admin / portal replay page. */
(function () {
    'use strict';

    function init() {
        if (typeof rrwebPlayer !== 'function'
            && typeof window.rrwebPlayer !== 'function') {
            // Player bundle hasn't loaded yet — try again on next tick.
            return setTimeout(init, 100);
        }
        var target = document.getElementById('player-container');
        if (!target) { return; }

        var raw;
        var dataEl = document.getElementById('recording-events');
        if (!dataEl) { return; }
        try {
            raw = JSON.parse(dataEl.textContent);
        } catch (e) {
            target.textContent = 'Could not parse the recording.';
            return;
        }

        // json_script + JSON.dumps((..., default=str)) leaves us with
        // the events list as a JS string — parse once more if needed.
        if (typeof raw === 'string') {
            try { raw = JSON.parse(raw); } catch (e) { /* keep */ }
        }
        if (!Array.isArray(raw) || raw.length === 0) {
            target.textContent = 'No events to replay.';
            return;
        }

        var Player = window.rrwebPlayer || rrwebPlayer;
        new Player({
            target: target,
            props: {
                events: raw,
                width: 1024,
                height: 576,
                autoPlay: false,
                showController: true,
                speedOption: [1, 2, 4, 8]
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
