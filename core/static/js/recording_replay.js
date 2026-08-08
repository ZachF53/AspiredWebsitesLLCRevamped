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
 *   scrubber: #replayer-scrub (role=slider) containing
 *             .replayer-scrub__played and .replayer-scrub__handle,
 *             plus #time-current / #time-total readouts
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
                // Short trail in the brand orange so the cursor's
                // path is easy to follow without becoming clutter.
                mouseTail: {
                    duration: 500,
                    lineCap: 'round',
                    lineWidth: 3,
                    strokeStyle: '#E8650A'
                }
            });
        } catch (e) {
            stage.innerHTML = renderEmptyState(
                'Replayer failed to initialise: ' + (e.message || e));
            return;
        }

        // ── FIT-TO-WIDTH ──
        // rrweb mounts an iframe sized to the captured viewport
        // (often 1920x1080+). Without scaling, the replay overflows
        // the stage horizontally. We compute the scale from the
        // captured viewport dimensions (which live on the Meta event
        // at the start of the stream — type=4, data.width/height)
        // rather than measuring the wrapper, because rrweb's wrapper
        // can briefly report stale offsetWidth before the iframe
        // settles.
        var captureW = 0, captureH = 0;
        for (var mi = 0; mi < events.length; mi++) {
            var ev = events[mi];
            if (ev && ev.type === 4 && ev.data &&
                ev.data.width && ev.data.height) {
                captureW = ev.data.width;
                captureH = ev.data.height;
                break;
            }
        }

        function fitToWidth() {
            var wrapper = stage.querySelector('.replayer-wrapper');
            if (!wrapper || !captureW || !captureH) { return; }
            var cs = getComputedStyle(stage);
            var padX = parseFloat(cs.paddingLeft) +
                       parseFloat(cs.paddingRight);
            var padY = parseFloat(cs.paddingTop) +
                       parseFloat(cs.paddingBottom);
            var available = stage.clientWidth - padX;
            if (available <= 0) { return; }
            var scale = Math.min(1, available / captureW);
            // Pin the wrapper to the captured viewport so the iframe
            // inside has a container that matches its intrinsic size.
            // Without this, rrweb sometimes leaves the wrapper at
            // width:auto while the iframe is 1920px wide, and the
            // iframe overflows the wrapper regardless of our scale.
            wrapper.style.width = captureW + 'px';
            wrapper.style.height = captureH + 'px';
            wrapper.style.transform = 'scale(' + scale + ')';
            // Also size the iframe child explicitly — belt-and-
            // suspenders for the same overflow case above.
            var iframe = wrapper.querySelector('iframe');
            if (iframe) {
                iframe.style.width = captureW + 'px';
                iframe.style.height = captureH + 'px';
            }
            // Collapse the empty space the scaled wrapper leaves
            // behind. Clear min-height (set in CSS for the loading
            // state) so a short captured viewport doesn't leave
            // white space below the iframe.
            stage.style.minHeight = '0';
            stage.style.height = (captureH * scale + padY) + 'px';
        }

        // The wrapper is created asynchronously by the Replayer —
        // poll for up to 1s. Once found, fit immediately, on window
        // resize, and on rrweb's own resize events (captured page
        // resized mid-session — pulls new width/height from payload).
        var fitTries = 0;
        function tryFit() {
            if (stage.querySelector('.replayer-wrapper')) {
                fitToWidth();
                window.addEventListener('resize', fitToWidth);
                try {
                    replayer.on('resize', function (payload) {
                        if (payload && payload.width && payload.height) {
                            captureW = payload.width;
                            captureH = payload.height;
                        }
                        requestAnimationFrame(fitToWidth);
                    });
                } catch (e) { /* older rrweb without on() — ignore */ }
                return;
            }
            if (fitTries++ < 20) { setTimeout(tryFit, 50); }
        }
        tryFit();

        // ── TRANSPORT STATE ──
        // rrweb has no "am I playing" getter we can rely on across
        // versions, so we track it ourselves and keep it in sync via
        // the replayer's own event emitter below.
        var playing = false;

        function setPlaying(v) {
            playing = v;
            if (stage) {
                stage.classList.toggle('is-playing', v);
            }
        }

        function doPlay(at) {
            try {
                if (typeof at === 'number') { replayer.play(at); }
                else { replayer.play(); }
                setPlaying(true);
            } catch (e) { /* ignore */ }
        }

        function doPause(at) {
            try {
                // pause(offset) seeks to that offset and renders the
                // frame there; pause() with no argument just stops.
                if (typeof at === 'number') { replayer.pause(at); }
                else { replayer.pause(); }
                setPlaying(false);
            } catch (e) { /* ignore */ }
        }

        wire('play-btn', function () { doPlay(); });
        wire('pause-btn', function () { doPause(); });
        wire('restart-btn', function () { doPlay(0); });

        var speedSelect = document.getElementById('speed-select');
        if (speedSelect) {
            speedSelect.addEventListener('change', function () {
                var s = parseFloat(speedSelect.value) || 1;
                replayer.setConfig({ speed: s });
            });
        }

        initScrubber(replayer, stage, {
            isPlaying: function () { return playing; },
            play: doPlay,
            pause: doPause,
            // Playback ran off the end — clear the flag without asking
            // rrweb to seek anywhere.
            markStopped: function () { setPlaying(false); }
        });

        // Auto-play. The custom controls work fine if user pauses.
        doPlay();
    }

    /*
     * YouTube-style seek bar.
     *
     * Drag semantics: pressing the track pauses playback and scrubs
     * frame-by-frame under the pointer; releasing resumes only if we
     * were playing when the drag started. Seeks are coalesced onto one
     * animation frame — a raw pointermove stream would ask rrweb to
     * rebuild the DOM dozens of times a second and stutter badly on
     * long recordings.
     */
    function initScrubber(replayer, stage, transport) {
        var scrub = document.getElementById('replayer-scrub');
        if (!scrub) { return; }

        var played = scrub.querySelector('.replayer-scrub__played');
        var handle = scrub.querySelector('.replayer-scrub__handle');
        var curEl = document.getElementById('time-current');
        var totEl = document.getElementById('time-total');

        var total = 0;
        try {
            var meta = replayer.getMetaData() || {};
            total = Math.max(0, meta.totalTime || 0);
        } catch (e) { total = 0; }

        if (totEl) { totEl.textContent = formatTime(total); }
        scrub.setAttribute('aria-valuemax', String(Math.round(total / 1000)));

        // A bounced session (Meta + FullSnapshot only) has no duration
        // — there is nothing to seek through, so say so rather than
        // leaving a dead control the user will try to drag.
        if (total <= 0) {
            scrub.classList.add('is-disabled');
            scrub.setAttribute('aria-disabled', 'true');
            scrub.removeAttribute('tabindex');
            paint(0);
            return;
        }

        var dragging = false;
        var resumeAfterDrag = false;
        var pendingSeek = null;
        var seekQueued = false;

        function paint(t) {
            var pct = total > 0 ? Math.max(0, Math.min(1, t / total)) : 0;
            var css = (pct * 100) + '%';
            if (played) { played.style.width = css; }
            if (handle) { handle.style.left = css; }
            if (curEl) { curEl.textContent = formatTime(t); }
            scrub.setAttribute('aria-valuenow', String(Math.round(t / 1000)));
            scrub.setAttribute('aria-valuetext', formatTime(t) + ' of ' +
                formatTime(total));
        }

        function timeFromEvent(clientX) {
            var rect = scrub.getBoundingClientRect();
            if (rect.width <= 0) { return 0; }
            var pct = (clientX - rect.left) / rect.width;
            return Math.max(0, Math.min(1, pct)) * total;
        }

        // Coalesce drag seeks to one per animation frame.
        function queueSeek(t) {
            pendingSeek = t;
            paint(t);                    // move the bar immediately
            if (seekQueued) { return; }
            seekQueued = true;
            requestAnimationFrame(function () {
                seekQueued = false;
                if (pendingSeek === null) { return; }
                var target = pendingSeek;
                pendingSeek = null;
                transport.pause(target);  // render the frame at `target`
            });
        }

        scrub.addEventListener('pointerdown', function (e) {
            if (scrub.classList.contains('is-disabled')) { return; }
            e.preventDefault();
            dragging = true;
            resumeAfterDrag = transport.isPlaying();
            scrub.classList.add('is-dragging');
            try { scrub.setPointerCapture(e.pointerId); } catch (err) { /* ok */ }
            queueSeek(timeFromEvent(e.clientX));
        });

        scrub.addEventListener('pointermove', function (e) {
            if (!dragging) { return; }
            e.preventDefault();
            queueSeek(timeFromEvent(e.clientX));
        });

        function endDrag(e) {
            if (!dragging) { return; }
            dragging = false;
            scrub.classList.remove('is-dragging');
            try { scrub.releasePointerCapture(e.pointerId); } catch (err) { /* ok */ }
            var t = timeFromEvent(e.clientX);
            pendingSeek = null;
            paint(t);
            if (resumeAfterDrag) { transport.play(t); }
            else { transport.pause(t); }
        }
        scrub.addEventListener('pointerup', endDrag);
        scrub.addEventListener('pointercancel', endDrag);

        // Keyboard seeking — arrows step 5s, page keys 30s.
        scrub.addEventListener('keydown', function (e) {
            var step = 0;
            switch (e.key) {
                case 'ArrowLeft':  step = -5000; break;
                case 'ArrowRight': step = 5000; break;
                case 'PageDown':   step = -30000; break;
                case 'PageUp':     step = 30000; break;
                case 'Home':       step = -Infinity; break;
                case 'End':        step = Infinity; break;
                default: return;
            }
            e.preventDefault();
            var now = currentTime();
            var t = Math.max(0, Math.min(total, now + step));
            paint(t);
            if (transport.isPlaying()) { transport.play(t); }
            else { transport.pause(t); }
        });

        function currentTime() {
            try {
                return Math.max(0, Math.min(total, replayer.getCurrentTime()));
            } catch (e) { return 0; }
        }

        // Follow playback. rAF rather than setInterval so the bar stays
        // glued to the frame rate and pauses with the tab.
        function tick() {
            if (!dragging && transport.isPlaying()) {
                paint(currentTime());
            }
            requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);

        // rrweb fast-forwards through idle gaps (skipInactive), which
        // makes the bar lurch. Flag it so the jump reads as intentional.
        try {
            replayer.on('skip-start', function () {
                scrub.classList.add('is-skipping');
            });
            replayer.on('skip-end', function () {
                scrub.classList.remove('is-skipping');
            });
            replayer.on('finish', function () {
                scrub.classList.remove('is-skipping');
                paint(total);
                transport.markStopped();
            });
        } catch (e) { /* older rrweb without on() — ignore */ }

        paint(0);
    }

    function formatTime(ms) {
        var total = Math.max(0, Math.round((ms || 0) / 1000));
        var m = Math.floor(total / 60);
        var s = total % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
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
