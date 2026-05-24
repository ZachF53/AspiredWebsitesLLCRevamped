/*
 * Boots the rrweb Replayer on the admin / portal replay page.
 * Uses the raw Replayer API (not the rrweb-player wrapper) for
 * reliability — the wrapper has been blank on real recordings.
 *
 * Required in the host page:
 *   <div id="replayer"></div>
 *   {{ events_json|json_script:"recording-events" }}
 *   buttons:  #play-btn  #pause-btn  #restart-btn
 *   select:   #speed-select  with value="0.5|1|2|4|8"
 */
(function () {
    'use strict';

    function init() {
        if (!window.rrweb || typeof rrweb.Replayer !== 'function') {
            // rrweb.min.js hasn't finished loading — try again.
            return setTimeout(init, 100);
        }

        var stage = document.getElementById('replayer');
        var dataEl = document.getElementById('recording-events');
        if (!stage || !dataEl) { return; }

        var events;
        try {
            events = JSON.parse(dataEl.textContent);
        } catch (e) {
            stage.innerHTML = renderEmptyState(
                'Could not parse the recording payload.');
            return;
        }
        if (typeof events === 'string') {
            try { events = JSON.parse(events); } catch (e) { /* keep */ }
        }
        if (!Array.isArray(events) || events.length === 0) {
            stage.innerHTML = renderEmptyState(
                'No recording events found. The recording may ' +
                'still be in progress.');
            return;
        }

        // rrweb's Replayer requires at least one FullSnapshot
        // (type=2) and one Meta (type=4) event to render the
        // baseline DOM. Without those, the player shows blank
        // controls over an empty stage. Detect that up-front and
        // explain what happened instead of silently showing nothing.
        var hasFullSnapshot = false;
        for (var i = 0; i < events.length; i++) {
            if (events[i] && events[i].type === 2) {
                hasFullSnapshot = true;
                break;
            }
        }
        if (!hasFullSnapshot) {
            stage.innerHTML = renderEmptyState(
                'Recording captured ' + events.length +
                ' interaction event' + (events.length === 1 ? '' : 's') +
                ', but the initial DOM snapshot is missing. ' +
                'This usually happens when the page is restored ' +
                'from the browser&rsquo;s back/forward cache before ' +
                'the recorder can re-baseline. The session timeline ' +
                'is still stored — replay will work on the next ' +
                'fresh page-load recording.');
            return;
        }

        var replayer;
        try {
            replayer = new rrweb.Replayer(events, {
                root: stage,
                skipInactive: true,
                showWarning: false,
                showDebug: false,
                liveMode: false,
                mouseTail: false
            });
        } catch (e) {
            stage.innerHTML = renderEmptyState(
                'Replayer failed to initialise: ' + (e.message || e));
            return;
        }

        // ── FIT-TO-WIDTH ──
        // The Replayer mounts an iframe sized to the captured viewport
        // (often 1920x1080+). Without scaling, the replay overflows the
        // stage horizontally and the user has to scroll. Apply a CSS
        // scale transform to .replayer-wrapper so the iframe always
        // fits the stage width. transform-origin is top-left (set in
        // CSS) so the scale shrinks from the corner.
        function fitToWidth() {
            var wrapper = stage.querySelector('.replayer-wrapper');
            if (!wrapper) { return; }
            // Reset transform so we measure the natural (captured) size.
            wrapper.style.transform = '';
            var available = stage.clientWidth -
                            (parseFloat(getComputedStyle(stage).paddingLeft)
                             + parseFloat(getComputedStyle(stage).paddingRight));
            var natural = wrapper.offsetWidth;
            if (!natural || !available) { return; }
            var scale = Math.min(1, available / natural);
            wrapper.style.transform = 'scale(' + scale + ')';
            // Collapse the empty space the scaled iframe leaves behind:
            // set the stage's height to the scaled wrapper height + the
            // existing vertical padding. min-height in CSS still acts
            // as a floor for very short captures.
            var paddingY = parseFloat(getComputedStyle(stage).paddingTop)
                         + parseFloat(getComputedStyle(stage).paddingBottom);
            stage.style.height = (wrapper.offsetHeight * scale + paddingY) + 'px';
        }

        // The wrapper is created asynchronously by the Replayer — give
        // it a tick to mount, then keep checking for up to 1s. Once
        // found, fit immediately, on every window resize, and on
        // rrweb's own resize events (captured page resized mid-session).
        var fitTries = 0;
        function tryFit() {
            if (stage.querySelector('.replayer-wrapper')) {
                fitToWidth();
                window.addEventListener('resize', fitToWidth);
                try {
                    replayer.on('resize', function () {
                        // The wrapper's intrinsic size has just changed —
                        // recompute on the next frame so layout has settled.
                        requestAnimationFrame(fitToWidth);
                    });
                } catch (e) { /* older rrweb without on() — ignore */ }
                return;
            }
            if (fitTries++ < 20) { setTimeout(tryFit, 50); }
        }
        tryFit();

        // Wire the simple custom controls.
        wire('play-btn', function () { replayer.play(); });
        wire('pause-btn', function () { replayer.pause(); });
        wire('restart-btn', function () { replayer.play(0); });

        var speedSelect = document.getElementById('speed-select');
        if (speedSelect) {
            speedSelect.addEventListener('change', function () {
                var s = parseFloat(speedSelect.value) || 1;
                replayer.setConfig({ speed: s });
            });
        }

        // Auto-play. The custom controls work fine if user pauses.
        try { replayer.play(); } catch (e) { /* ignore */ }
    }

    function wire(id, handler) {
        var el = document.getElementById(id);
        if (el) { el.addEventListener('click', handler); }
    }

    function renderEmptyState(msg) {
        return (
            '<div class="replayer-empty">' +
            '<p>' + msg + '</p>' +
            '</div>'
        );
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
