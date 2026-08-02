/*
 * GA4 conversion events for the marketing site.
 * Master Plan §10 · MEASUREMENT_SPEC §5.
 *
 * Loaded after analytics.js, which defines window.gtag. External file,
 * no inline handlers anywhere, so the public CSP keeps script-src at
 * 'self' + googletagmanager.com. No new hosts are needed.
 *
 * Two sources of events:
 *
 *   1. Server-queued — a JSON payload in #analytics-events, written by
 *      core.analytics when a conversion is confirmed server-side (a
 *      Lead row was really created, an audit really ran). Emitted on
 *      load.
 *
 *   2. Click and scroll behaviour, below.
 *
 * The click events are derived from the DOM rather than hand-tagged
 * with data attributes on every button. There are dozens of CTAs across
 * the service, location, pricing and case-study pages, and any page
 * added later would need remembering. Deriving from href and position
 * means a new page is instrumented the moment it is written.
 *
 * PII (§5.3): no event carries an email address, a name, or the digits
 * of a phone number. `phone_tap` reports that a tel: link was tapped
 * and on which page — never who tapped it, and not the number, which
 * is ours anyway and identical everywhere.
 */
(function () {
    'use strict';

    function emit(name, params) {
        // gtag is absent when GOOGLE_ANALYTICS_ID is blank — staging and
        // local dev. Checked per-call rather than once at startup so
        // script execution order can never matter.
        if (typeof window.gtag !== 'function') { return; }
        window.gtag('event', name, params || {});
    }

    function path() {
        return window.location.pathname;
    }

    /* ── 1. Server-queued conversions ─────────────────────────── */

    var payload = document.getElementById('analytics-events');
    if (payload) {
        var queued = [];
        try {
            queued = JSON.parse(payload.textContent) || [];
        } catch (err) {
            queued = [];
        }
        queued.forEach(function (ev) {
            if (ev && ev.name) { emit(ev.name, ev.params); }
        });
    }

    /* ── 2. Click events ──────────────────────────────────────── */

    // Where on the page a CTA sits. §5 wants hero/footer/inline for
    // booking_click so a hero test can be read separately from the
    // footer's standing link.
    function ctaLocation(el) {
        if (el.closest('.hero')) { return 'hero'; }
        if (el.closest('footer')) { return 'footer'; }
        if (el.closest('nav')) { return 'nav'; }
        return 'inline';
    }

    // A booking CTA, as §5 defines it: the "Book a Free Strategy Call"
    // button. All 22 of those point at /contact/ rather than the
    // scheduler, so the destination alone cannot identify them — the
    // header's plain "Contact" link has the same href and is not a
    // booking click. Match the CTA wording, which is what the spec
    // actually names.
    var BOOKING_TEXT = /\b(book|schedule)\b/i;

    function isBookingCta(link, href) {
        if (href.indexOf('/design/schedule/') === 0) { return true; }
        return href.indexOf('/contact/') === 0
            && BOOKING_TEXT.test(link.textContent || '');
    }

    // The pricing card a CTA belongs to, by its visible title — that is
    // the tier name a human reads on the page, so the report matches
    // what was clicked even if slugs change.
    function tierName(el) {
        var card = el.closest('.card--pricing');
        if (!card) { return null; }
        var title = card.querySelector('.card__title');
        return title ? title.textContent.trim() : null;
    }

    function caseStudySlug() {
        var m = window.location.pathname.match(/^\/portfolio\/([^/]+)\/?$/);
        return m ? m[1] : null;
    }

    document.addEventListener('click', function (e) {
        var link = e.target.closest('a');
        if (!link) { return; }
        var href = link.getAttribute('href') || '';

        if (href.indexOf('tel:') === 0) {
            emit('phone_tap', {
                page_path: path(),
                // Coarse form factor, not a device fingerprint.
                device: window.matchMedia('(max-width: 719px)').matches
                    ? 'mobile' : 'desktop'
            });
            return;
        }

        if (href.indexOf('mailto:') === 0) {
            emit('email_click', { page_path: path() });
            return;
        }

        if (isBookingCta(link, href)) {
            var booking = {
                page_path: path(),
                cta_location: ctaLocation(link)
            };
            var tier = tierName(link);
            if (tier) { booking.tier_name = tier; }
            emit('booking_click', booking);
            // Deliberately no `return`. A scheduler link inside a
            // pricing card is both a booking and a pricing CTA, and
            // the build tiers are call-first — they never self-
            // checkout. Returning here would leave /pricing/ reporting
            // permanently blind to the two highest-value products.
        }

        var tierClicked = tierName(link);
        if (tierClicked) {
            emit('pricing_cta_click', {
                tier_name: tierClicked,
                page_path: path()
            });
            return;
        }

        // Any button on a case-study page. The page exists to send a
        // reader onward, so every button on it is that page's CTA.
        var slug = caseStudySlug();
        if (slug && (link.classList.contains('btn-primary')
                || link.classList.contains('btn-secondary'))) {
            emit('case_study_cta_click', {
                case_study_slug: slug,
                page_path: path()
            });
        }
    });

    /* ── 3. Scroll depth ──────────────────────────────────────── */

    var fired90 = false;
    function onScroll() {
        if (fired90) { return; }
        var doc = document.documentElement;
        var scrollable = doc.scrollHeight - window.innerHeight;
        // A page shorter than the viewport has nothing to scroll; it is
        // not a 90% read, so it does not count.
        if (scrollable <= 0) { return; }
        if ((window.scrollY / scrollable) >= 0.9) {
            fired90 = true;
            emit('scroll_90', { page_path: path() });
            window.removeEventListener('scroll', onScroll);
        }
    }
    window.addEventListener('scroll', onScroll, { passive: true });
})();
