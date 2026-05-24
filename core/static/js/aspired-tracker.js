/*
 * Aspired Websites — Conversion & Analytics
 * Tracker v2.0 (Tier 1)
 *
 * Tracks: form submissions, phone clicks,
 * CTA clicks, scroll depth milestones,
 * click coordinates, time on page,
 * exit intent signals.
 *
 * All data batched and sent on page unload.
 * No cookies. No PII. No external requests.
 *
 * <script src="...aspired-tracker.js"
 *   data-aspired-client="UUID" defer></script>
 */
(function () {
  'use strict';

  var BATCH_ENDPOINT =
    'https://aspiredwebsites.com/api/track/batch/';

  var tag = document.currentScript ||
    document.querySelector('script[data-aspired-client]');
  var CLIENT_ID = tag ? tag.getAttribute('data-aspired-client') : '';
  if (!CLIENT_ID) { return; }

  // ── STATE ──────────────────────────────
  var sessionId = generateSessionId();
  var pageStartTime = Date.now();
  var maxScrollDepth = 0;
  var scrollMilestones = [25, 50, 75, 90, 100];
  var scrollMilestonesHit = [];
  var exitIntentFired = false;
  var eventQueue = [];
  var clickHeatmap = [];
  // List of {x_pct, y_pct, tag, text, ts}

  // ── HELPERS ────────────────────────────

  function generateSessionId() {
    // Random session ID — not stored in
    // cookies, valid for this page view only.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'
      .replace(/[xy]/g, function (c) {
        var r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8))
          .toString(16);
      });
  }

  function getScrollDepth() {
    var scrollTop = window.pageYOffset ||
      document.documentElement.scrollTop || 0;
    var docHeight = Math.max(
      document.documentElement.scrollHeight,
      document.body.scrollHeight
    ) - window.innerHeight;
    if (docHeight <= 0) { return 100; }
    return Math.round(
      Math.min((scrollTop / docHeight) * 100, 100)
    );
  }

  function getPageHeight() {
    return Math.max(
      document.documentElement.scrollHeight,
      document.body.scrollHeight
    );
  }

  function getClickPosition(e) {
    // Click position as percentage of page
    // width and total page height.
    var x = e.pageX ||
      (e.clientX + window.pageXOffset) || 0;
    var y = e.pageY ||
      (e.clientY + window.pageYOffset) || 0;
    var w = document.documentElement.offsetWidth
      || window.innerWidth || 1;
    var h = getPageHeight() || 1;
    return {
      x_pct: Math.round((x / w) * 100),
      y_pct: Math.round((y / h) * 100)
    };
  }

  function assign(target, source) {
    // Lightweight Object.assign polyfill so this
    // works on older browsers that get the
    // tracker via a client-installed snippet.
    if (!source) { return target; }
    for (var k in source) {
      if (Object.prototype.hasOwnProperty.call(source, k)) {
        target[k] = source[k];
      }
    }
    return target;
  }

  function queueEvent(eventType, data) {
    eventQueue.push(assign({
      client_id: CLIENT_ID,
      session_id: sessionId,
      event_type: eventType,
      page_url: window.location.href,
      page_title: document.title,
      timestamp: new Date().toISOString()
    }, data || {}));
  }

  var batchSent = false;
  function sendBatch() {
    // Called on page unload — sends all queued
    // events in one request. Guards against the
    // multiple invocations that pagehide +
    // beforeunload + visibilitychange can fire.
    if (batchSent) { return; }
    batchSent = true;
    if (!eventQueue.length &&
        maxScrollDepth === 0 &&
        clickHeatmap.length === 0) {
      // Nothing happened on this page — skip.
      return;
    }

    var timeOnPage = Math.round(
      (Date.now() - pageStartTime) / 1000
    );

    // Add the page summary event last so the
    // server can find it with a single scan.
    eventQueue.push({
      client_id: CLIENT_ID,
      session_id: sessionId,
      event_type: 'page_summary',
      page_url: window.location.href,
      page_title: document.title,
      timestamp: new Date().toISOString(),
      time_on_page_seconds: timeOnPage,
      max_scroll_depth: maxScrollDepth,
      scroll_milestones_hit: scrollMilestonesHit,
      exit_intent_fired: exitIntentFired,
      click_heatmap: clickHeatmap.slice(0, 50)
      // Cap at 50 clicks per page.
    });

    var body = JSON.stringify({
      client_id: CLIENT_ID,
      session_id: sessionId,
      events: eventQueue
    });

    if (navigator.sendBeacon) {
      try {
        navigator.sendBeacon(BATCH_ENDPOINT, body);
      } catch (e) { /* swallow */ }
    } else {
      try {
        fetch(BATCH_ENDPOINT, {
          method: 'POST',
          body: body,
          keepalive: true
        });
      } catch (e) { /* swallow */ }
    }
  }

  // ── SCROLL TRACKING ────────────────────

  var scrollTicking = false;
  function onScroll() {
    if (scrollTicking) { return; }
    scrollTicking = true;
    var raf = window.requestAnimationFrame ||
      function (cb) { return setTimeout(cb, 16); };
    raf(function () {
      var depth = getScrollDepth();
      if (depth > maxScrollDepth) {
        maxScrollDepth = depth;
      }
      for (var i = 0; i < scrollMilestones.length; i++) {
        var m = scrollMilestones[i];
        if (depth >= m &&
            scrollMilestonesHit.indexOf(m) === -1) {
          scrollMilestonesHit.push(m);
          queueEvent('scroll_milestone', {
            milestone: m
            // e.g. 50 = "reached 50% of page"
          });
        }
      }
      scrollTicking = false;
    });
  }

  window.addEventListener('scroll', onScroll,
    { passive: true });

  // ── CLICK HEATMAP ──────────────────────

  document.addEventListener('click', function (e) {
    var pos = getClickPosition(e);
    var el = e.target;
    clickHeatmap.push({
      x_pct: pos.x_pct,
      y_pct: pos.y_pct,
      tag: ((el && el.tagName) || '').toLowerCase(),
      text: ((el && el.innerText) || '')
        .slice(0, 50).trim(),
      ts: Date.now() - pageStartTime
      // ms since page load
    });
  }, { passive: true });

  // ── EXIT INTENT ────────────────────────
  // Fires when the mouse moves toward the top
  // of the browser (toward close/back button).

  document.addEventListener('mouseleave', function (e) {
    if (e.clientY <= 5 && !exitIntentFired) {
      exitIntentFired = true;
      var timeOnPage = Math.round(
        (Date.now() - pageStartTime) / 1000
      );
      queueEvent('exit_intent', {
        time_on_page_seconds: timeOnPage,
        scroll_depth_at_exit: maxScrollDepth
      });
    }
  });

  // ── FORM SUBMISSIONS ───────────────────

  document.addEventListener('submit', function (e) {
    if (e.target) {
      queueEvent('form_submit', {
        element_id: e.target.id || '',
        form_action: e.target.action || ''
      });
    }
  });

  // ── PHONE CLICKS ───────────────────────

  document.addEventListener('click', function (e) {
    var link = (e.target && e.target.closest)
      ? e.target.closest('a') : null;
    if (link && link.href &&
        link.href.indexOf('tel:') === 0) {
      queueEvent('phone_click', {
        phone_number: link.href.replace('tel:', ''),
        element_text: (link.innerText || '')
          .slice(0, 50)
      });
    }
  });

  // ── CTA CLICKS ─────────────────────────

  var CTA_PATTERNS = [
    'contact', 'call', 'schedule', 'book',
    'consultation', 'free', 'get started',
    'learn more', 'request', 'quote',
    'appointment', 'hire', 'work with'
  ];

  document.addEventListener('click', function (e) {
    var btn = (e.target && e.target.closest)
      ? e.target.closest('button, a, [role="button"]')
      : null;
    if (!btn) { return; }
    var text = (btn.innerText || '').toLowerCase();
    for (var i = 0; i < CTA_PATTERNS.length; i++) {
      if (text.indexOf(CTA_PATTERNS[i]) !== -1) {
        queueEvent('cta_click', {
          element_id: btn.id || '',
          element_text: (btn.innerText || '')
            .slice(0, 100),
          element_href: btn.href || ''
        });
        return;
      }
    }
  });

  // ── PAGE VISIBILITY ────────────────────
  // Mobile browsers often suspend pages on
  // tab switch without firing pagehide — ship
  // the batch whenever visibility drops.

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      sendBatch();
    }
  });

  // ── SEND ON UNLOAD ─────────────────────

  // pagehide is more reliable than unload for
  // back/forward cache compatibility.
  window.addEventListener('pagehide', sendBatch);
  // Fallback for older browsers.
  window.addEventListener('beforeunload', sendBatch);

  // Also send after 30s on page in case they
  // leave without triggering any unload event.
  setTimeout(function () {
    if (eventQueue.length > 0 ||
        maxScrollDepth > 0 ||
        clickHeatmap.length > 0) {
      sendBatch();
      // Reset the guard so anything new before
      // the real unload still ships.
      batchSent = false;
      eventQueue = [];
    }
  }, 30000);

  // ── TIER 2: SESSION RECORDING (rrweb) ──
  // Only activates when data-tier="2" is set
  // on the script tag. Self-hosts the rrweb
  // recorder from aspiredwebsites.com so no
  // 3rd-party CDN call is made from the
  // client's site. Privacy: all inputs are
  // masked, [data-recording-block] elements
  // are skipped entirely.

  var TIER = tag ? (tag.getAttribute('data-tier') || '1') : '1';
  var RECORDING_ENDPOINT =
    'https://aspiredwebsites.com/api/track/recording/';

  if (TIER === '2') {
    var rrwebScript = document.createElement('script');
    rrwebScript.src =
      'https://aspiredwebsites.com/static/js/' +
      'rrweb-record.min.js';
    rrwebScript.defer = true;
    rrwebScript.onload = function () { startRecording(); };
    document.head.appendChild(rrwebScript);
  }

  function startRecording() {
    // rrweb-record self-bundle exposes
    // `window.rrwebRecord` (a callable). Some
    // older builds expose `window.rrweb.record`
    // — check both so this keeps working if we
    // upgrade the bundle.
    var record = (window.rrwebRecord
                  || (window.rrweb && window.rrweb.record));
    if (typeof record !== 'function') { return; }

    var recordingBuffer = [];
    var stopRecording = record({
      emit: function (event) {
        recordingBuffer.push(event);
        // Ship in 100-event chunks so a sudden
        // close doesn't lose the whole session.
        if (recordingBuffer.length >= 100) {
          sendRecordingChunk(
            recordingBuffer.splice(0, 100), false);
        }
      },
      // ── PRIVACY (do not change without review) ──
      maskAllInputs: true,
      // Never record what the user types.
      maskInputOptions: {
        password: true,
        email: true,
        tel: true,
        text: true,
        number: true,
        search: true,
        textarea: true
      },
      // Add data-recording-block to any element
      // that should never be recorded (sensitive
      // content blocks, dashboards, etc.).
      blockSelector: '[data-recording-block]',
      // Sample mousemove every 50ms — captures
      // gesture intent without ballooning data.
      sampling: {
        mousemove: 50,
        mouseInteraction: true,
        scroll: 150,
        media: 800
      }
    });

    function sendRecordingChunk(events, isFinal) {
      if (!events || !events.length) { return; }
      var body = JSON.stringify({
        client_id: CLIENT_ID,
        session_id: sessionId,
        page_url: window.location.href,
        page_title: document.title,
        events: events,
        is_final: !!isFinal,
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight
        },
        timestamp: new Date().toISOString()
      });

      if (isFinal && navigator.sendBeacon) {
        try {
          navigator.sendBeacon(RECORDING_ENDPOINT, body);
        } catch (e) { /* swallow */ }
      } else {
        try {
          fetch(RECORDING_ENDPOINT, {
            method: 'POST',
            body: body,
            headers: { 'Content-Type': 'application/json' },
            keepalive: isFinal
          }).catch(function () { /* swallow */ });
        } catch (e) { /* swallow */ }
      }
    }

    // Final chunk on page unload.
    window.addEventListener('pagehide', function () {
      try { if (stopRecording) { stopRecording(); } } catch (e) {}
      sendRecordingChunk(recordingBuffer.splice(0), true);
    });
    window.addEventListener('beforeunload', function () {
      sendRecordingChunk(recordingBuffer.splice(0), true);
    });
  }
})();
