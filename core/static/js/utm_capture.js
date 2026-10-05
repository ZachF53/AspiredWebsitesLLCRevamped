/*
 * UTM / attribution capture — claude-code-website-spec.md Job 2.
 *
 * First touch: captured once on first visit, written to localStorage,
 * never overwritten again. Kept for later even though the contact
 * form only sends last touch (more actionable for a solo operator).
 *
 * Last touch: current URL's params, refreshed on every page load,
 * written to sessionStorage. This is what gets sent with the form.
 *
 * Every storage read/write is wrapped in try/catch — private browsing
 * and blocked site data both throw. A failure here must degrade to
 * empty hidden fields, never a broken page or a broken form.
 */
(function () {
    'use strict';

    var FIRST_TOUCH_KEY = 'aspired_first_touch';
    var LAST_TOUCH_KEY = 'aspired_last_touch';

    var FIELDS = [
        'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
        'utm_content', 'gclid', 'fbclid',
    ];

    function readCurrentTouch() {
        var params;
        try {
            params = new URLSearchParams(window.location.search);
        } catch (e) {
            params = null;
        }
        var touch = {};
        FIELDS.forEach(function (key) {
            touch[key] = (params && params.get(key)) || '';
        });
        touch.landing_page = window.location.href;
        touch.referrer = document.referrer || '';
        return touch;
    }

    function hasAnyValue(touch) {
        return FIELDS.some(function (key) {
            return touch[key];
        });
    }

    function storeFirstTouchOnce(touch) {
        try {
            if (window.localStorage.getItem(FIRST_TOUCH_KEY)) {
                return;
            }
            // Only worth recording if the visit actually carries
            // attribution — a bare homepage visit with no params
            // shouldn't claim "first touch" over a later campaign hit.
            if (hasAnyValue(touch) || touch.referrer) {
                window.localStorage.setItem(
                    FIRST_TOUCH_KEY, JSON.stringify(touch));
            }
        } catch (e) {
            /* private browsing / blocked storage — ignore */
        }
    }

    function storeLastTouch(touch) {
        try {
            window.sessionStorage.setItem(
                LAST_TOUCH_KEY, JSON.stringify(touch));
        } catch (e) {
            /* ignore */
        }
    }

    function readLastTouch() {
        try {
            var raw = window.sessionStorage.getItem(LAST_TOUCH_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    }

    function populateHiddenFields(touch) {
        touch = touch || {};
        var ids = FIELDS.concat(['landing_page', 'referrer']);
        ids.forEach(function (id) {
            var el = document.getElementById(id);
            if (el) {
                el.value = touch[id] || '';
            }
        });
    }

    var currentTouch = readCurrentTouch();
    storeFirstTouchOnce(currentTouch);
    // Last touch only overwrites when the current visit actually carries
    // new attribution — otherwise a page view with no UTM params (e.g.
    // navigating the site after arriving) would blank out the campaign
    // that brought the visitor in a few clicks ago.
    if (hasAnyValue(currentTouch)) {
        storeLastTouch(currentTouch);
    } else if (!readLastTouch()) {
        storeLastTouch(currentTouch);
    }

    document.addEventListener('DOMContentLoaded', function () {
        populateHiddenFields(readLastTouch() || currentTouch);
    });
})();
