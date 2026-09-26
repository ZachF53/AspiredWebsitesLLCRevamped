"""
Expanded case-study content (Sept 2026): the measured scorecard and the
technical deep dive for each published portfolio project.

Every point here was checked on 2026-09-26 against the live site (page
source, response headers, sitemap, structured data), Lighthouse mobile
runs (median of three), our own uptime monitor and monthly security
scans, or the project's source code. Nothing is estimated. Rules used:

  * Lighthouse score circles are published only when all four mobile
    categories are 90+; otherwise the page shows other measured facts.
  * A security header is claimed only if the live site sends it.
  * Denis Law Group is a maintained WordPress site we did not build, so
    its section describes the site as it stands, not as our work.

Consumed by clients/migrations/0073_case_study_deep_dive_content.py
(fills empty fields on existing rows) and seed_case_studies (fresh
installs). Edit a live row in the admin; this module only seeds.
"""

MEASURED_ON = '2026-09-26'
LIGHTHOUSE_METHOD = (
    'Lighthouse 12, mobile emulation with throttling, median of three '
    'runs; uptime and security figures come from our own monitoring.')
FACTS_METHOD = (
    'Figures from the live site, Lighthouse 12 (mobile emulation), and '
    'our own uptime monitoring and security scans.')


CONTENT = {
    # ───────────────────────── Moonieful Designs ─────────────────────────
    'moonieful-designs': {
        'scorecard': {
            'measured_on': MEASURED_ON,
            'method': FACTS_METHOD,
            'vitals': [
                ['224 KB', 'Total homepage weight on a phone'],
                ['17', 'Requests to load the homepage'],
            ],
            'uptime': {'percent': '99.9%', 'checks': '20,263',
                       'since': 'May 22, 2026'},
            'security_scan': {'date': 'Sept 24, 2026', 'critical': 0,
                              'high': 0},
        },
        'deep_dive': [
            {
                'eyebrow': 'The Platform',
                'heading': 'A studio’s whole operating system, not just a portfolio',
                'intro': (
                    'The homepage is the storefront. Behind it is the '
                    'system Miki runs her studio on, built in Django and '
                    'developed across more than 240 commits since 2023.'),
                'points': [
                    'Client portal with project stages, tasks, meetings, '
                    'versioned files, approvals, change requests and '
                    'page-by-page website feedback with screenshots.',
                    'Intake forms Miki builds herself, including questions '
                    'that take file uploads.',
                    'A shop for her book, Brand Fog: an embedded Stripe '
                    'payment form, sales tax calculated by Stripe Tax, and '
                    'every buyer’s PDF stamped with their name.',
                    'Paid vendor listings billed quarterly through Stripe, '
                    'with a self-service billing portal.',
                    'An email outbox that retries failed sends on a '
                    'back-off schedule, across 21 email types.',
                    'A signed sync bridge to Aspired Websites, so a client '
                    'handed over for a website arrives with their brand '
                    'work intact.',
                ],
            },
            {
                'eyebrow': 'Speed',
                'heading': 'Light where it counts',
                'intro': (
                    'The homepage loads in 17 requests and 224 KB in '
                    'total, measured on a phone.'),
                'points': [
                    'Homepage styles are written into the page itself, so '
                    'there is no separate stylesheet to wait for.',
                    'The hero photo is a 41 KB WebP marked as the page’s '
                    'first priority; images further down load only as they '
                    'scroll into view.',
                    'Every homepage image carries its width and height, so '
                    'the browser reserves the space before it arrives.',
                    'One 2 KB script, deferred until the page has rendered.',
                    'Static files are fingerprinted, pre-compressed and '
                    'cached by browsers for a year.',
                ],
            },
            {
                'eyebrow': 'Mobile-First',
                'heading': 'Built for a thumb, not a mouse',
                'points': [
                    'Type and spacing scale fluidly with the screen instead '
                    'of jumping between fixed sizes.',
                    'A phone-only sticky booking bar that stays clear of the '
                    'iPhone home indicator; tap targets are at least 44 px.',
                    'The mobile menu announces its state to screen readers, '
                    'and Escape closes it and returns focus.',
                    'Layouts and contrast checked at 1440, 1024, 768 and '
                    '390 px wide, with a headless phone audit in the repo.',
                ],
            },
            {
                'eyebrow': 'SEO',
                'heading': 'Findable, and clean for search engines',
                'intro': (
                    'Lighthouse scores the homepage 100 for SEO, 100 for '
                    'accessibility and 100 for best practices on mobile.'),
                'points': [
                    'Unique titles, meta descriptions, canonical URLs and '
                    'Open Graph tags on the public pages.',
                    'A generated sitemap and robots.txt; the client portal '
                    'and admin are kept out of search results.',
                    'One H1 per page and proper landmarks (header, nav, '
                    'main, footer) so the structure reads the same to '
                    'Google as it does to a person.',
                    'Retired pages redirect instead of returning errors, '
                    'so old links never dead-end.',
                ],
            },
            {
                'eyebrow': 'Security',
                'heading': 'Locked down by default',
                'points': [
                    'A content security policy on the homepage and shop: '
                    'scripts may load only from the site itself and two '
                    'named partners, and plugins are blocked outright.',
                    'HTTPS only, with HSTS, TLS 1.3, and headers that stop '
                    'the site being framed or content-type sniffed.',
                    'File uploads are checked against an allowlist, capped '
                    'in size, verified as real images, and stored outside '
                    'the public web folder behind permission checks.',
                    'Payment webhooks are verified by signature, and card '
                    'details are handled by Stripe, never by the server.',
                ],
            },
            {
                'eyebrow': 'Accessibility',
                'heading': 'Usable by everyone',
                'points': [
                    'A skip-to-content link and a visible 3 px focus '
                    'outline, measured at 6.3:1 contrast.',
                    'Animation switches off for visitors who ask their '
                    'device to reduce motion.',
                    'Text colors are measured against their backgrounds '
                    '(body text at 13:1).',
                    'Alt text on every meaningful image; decorative images '
                    'are hidden from screen readers.',
                ],
            },
        ],
    },

    # ─────────────────────── Food Trucks of San Antonio ───────────────────────
    'food-trucks-of-san-antonio': {
        'scorecard': {
            'measured_on': MEASURED_ON,
            'method': FACTS_METHOD,
            'vitals': [
                ['21', 'Truck profiles built (8+ featured live at a time)'],
                ['34', 'Pages in the sitemap'],
                ['100', 'Lighthouse SEO score (mobile)'],
                ['0.04', 'Layout shift while loading (CLS)'],
            ],
            'security_scan': {'date': 'Aug 28, 2026', 'critical': 0,
                              'high': 0},
        },
        'deep_dive': [
            {
                'eyebrow': 'The Platform',
                'heading': 'A live directory, run by the trucks themselves',
                'intro': (
                    'Built in Django on PostgreSQL. Truck owners keep their '
                    'own profiles current, so the directory stays accurate '
                    'without an editor in the middle.'),
                'points': [
                    'A live Google map with color-coded pins: out now, '
                    'coming up, and out for the next one to two hours or '
                    'longer.',
                    'Search by name, cuisine or description, sorting, and '
                    'landing pages for each cuisine (BBQ, tacos, Cajun, '
                    'American, Caribbean and more).',
                    'Every truck profile carries its full menu with prices '
                    'and photos, accepted payment methods, reviews, and '
                    'related trucks.',
                    'Separate accounts for truck owners, food lovers and '
                    'local businesses; businesses can post a request to '
                    'book a truck for an event.',
                    'Favorites, reviews, a blog with a newsletter, and an '
                    'admin dashboard that tracks which trucks have claimed '
                    'and verified their profiles.',
                ],
            },
            {
                'eyebrow': 'SEO',
                'heading': 'Structured so Google understands every truck',
                'intro': 'Lighthouse scores the homepage 100 for SEO on mobile.',
                'points': [
                    'Every truck page is marked up as a Restaurant, with '
                    'its address and a breadcrumb trail, and titled for '
                    'the search people actually make (“Curbside Eats | '
                    'American Food Truck in San Antonio”).',
                    'The directory is an ItemList; the homepage carries '
                    'WebSite (with a site search action), Organization, '
                    'LocalBusiness and FAQPage structured data.',
                    'A generated sitemap covering all 34 public pages, and '
                    'a robots.txt that keeps account and dashboard pages '
                    'out of search.',
                    'Readable, slug-based URLs for every truck and '
                    'cuisine, and Open Graph tags so shared links preview '
                    'properly.',
                ],
            },
            {
                'eyebrow': 'Mobile-First',
                'heading': 'Made for people standing on a sidewalk',
                'points': [
                    'Critical styles are written into the page so the '
                    'first screen paints without waiting on a stylesheet; '
                    'layout shift while loading measures 0.04 (Google’s '
                    '“good” line is 0.1).',
                    'Most homepage images are WebP, and 22 of the 34 load '
                    'only as they scroll into view.',
                    'Photos are resized on the server when owners upload '
                    'them, so a phone never downloads a camera-sized image.',
                    'Tap-to-call and tap-to-email booking links on every '
                    'truck that takes events.',
                ],
            },
            {
                'eyebrow': 'Security',
                'heading': 'Open to everyone, closed to abuse',
                'points': [
                    'CSRF protection on every form, plus reCAPTCHA checked '
                    'on the server and hidden honeypot fields for bots.',
                    'Role-based access for each account type, and admin '
                    'controls to ban or lock an account.',
                    'HTTPS on TLS 1.3, with headers that stop the site being '
                    'framed or content-type sniffed.',
                    'Our most recent monthly security scan found no '
                    'critical or high-severity issues.',
                ],
            },
        ],
    },

    # ───────────────────────── Burgland Technologies ─────────────────────────
    'burgland-technologies': {
        'scorecard': {
            'measured_on': MEASURED_ON,
            'method': FACTS_METHOD,
            'vitals': [
                ['0', 'Layout shift while loading (CLS)'],
                ['115 ms', 'Average response time to our uptime monitor'],
                ['12', 'Pages, each with its own structured data'],
            ],
            'uptime': {'percent': '100%', 'checks': '20,076',
                       'since': 'May 23, 2026'},
            'security_scan': {'date': 'Sept 24, 2026', 'critical': 0,
                              'high': 0},
        },
        'deep_dive': [
            {
                'eyebrow': 'The Site',
                'heading': 'Built to be inspected by technical buyers',
                'intro': (
                    'A hand-coded Django site: hand-written CSS, a small '
                    'amount of plain JavaScript, no page builder and no '
                    'CSS framework.'),
                'points': [
                    'Six service pages (AI strategy and governance, '
                    'cybersecurity consulting, technology governance and '
                    'risk, program and project management, transformation, '
                    'and small-business technology advice), each with its '
                    'own FAQ.',
                    'An Insights blog the team publishes to directly from '
                    'the site, without a developer.',
                    'A contact page with a CSRF-protected form and '
                    'reCAPTCHA checked on the server.',
                ],
            },
            {
                'eyebrow': 'SEO',
                'heading': 'Every page tells Google exactly what it is',
                'points': [
                    'Each service page is marked up as a Service with an '
                    'FAQ, articles as BlogPosting with their author, and '
                    'the contact page as a ContactPage.',
                    'Breadcrumbs, the business address and opening hours '
                    'in structured data on every page.',
                    'Titles written for local search (“IT & AI Consulting '
                    'in San Antonio, TX”), with canonical URLs and '
                    'Open Graph tags.',
                    'A generated sitemap and a robots.txt that keeps the '
                    'admin, login and article editor out of search; every '
                    'address resolves to one canonical www host.',
                ],
            },
            {
                'eyebrow': 'Speed & Mobile',
                'heading': 'Steady on a phone',
                'points': [
                    'Nothing moves while the page loads: layout shift '
                    'measures 0.',
                    'The homepage HTML is 30 KB, with all styling in a '
                    'single hand-written stylesheet.',
                    'Eight of the ten homepage images load only as they '
                    'scroll into view.',
                    'The server answers our uptime monitor in 115 ms on '
                    'average.',
                ],
            },
            {
                'eyebrow': 'Security',
                'heading': 'The details a technical visitor checks',
                'points': [
                    'HTTPS on TLS 1.3, with plain-HTTP requests redirected.',
                    'Headers that stop the site being framed or '
                    'content-type sniffed, plus strict referrer and '
                    'cross-origin opener policies.',
                    'Forms protected against cross-site request forgery '
                    'and bots.',
                    '100% uptime across 20,076 checks since May 2026, and '
                    'no critical or high-severity findings in our latest '
                    'monthly security scan.',
                ],
            },
        ],
    },

    # ─────────────────────────── Whitehead Wellness ───────────────────────────
    'whitehead-wellness': {
        'scorecard': {
            'measured_on': MEASURED_ON,
            'method': FACTS_METHOD,
            'vitals': [
                ['0 ms', 'Total blocking time on a phone'],
                ['0.03', 'Layout shift while loading (CLS)'],
                ['100', 'Lighthouse SEO score (mobile)'],
            ],
            'uptime': {'percent': '100%', 'checks': '11,682',
                       'since': 'Aug 17, 2026'},
            'security_scan': {'date': 'Sept 20, 2026', 'critical': 0,
                              'high': 0},
        },
        'deep_dive': [
            {
                'eyebrow': 'The Membership Engine',
                'heading': 'Everything Kate needs to run it herself',
                'intro': (
                    'Built in Django with PostgreSQL, Celery and Redis, '
                    'covered by 336 automated tests.'),
                'points': [
                    'Four membership levels billed through Stripe, with '
                    'grandfathered pricing, capacity limits and a waitlist. '
                    'Downgrades and cancellations take effect at the end of '
                    'the billing period and can be undone until then.',
                    'A verification queue where Kate reviews each member’s '
                    'evidence, approves it or asks for changes, and every '
                    'earned crest creates a task to mail a physical card.',
                    'Daily ritual streaks counted at each member’s own '
                    'midnight, with grace days applied automatically.',
                    'A community with ten channels, reactions, reporting, '
                    'and one moderation queue for posts and comments.',
                    'Private coaching messages for the top level, with a '
                    'daily digest to Kate only on days she has unread '
                    'messages.',
                    'An admin panel with roles, permissions and an audit '
                    'log, and email wording Kate edits herself.',
                ],
            },
            {
                'eyebrow': 'Speed',
                'heading': 'Instant to the touch',
                'points': [
                    'Hand-written CSS and plain JavaScript: 15 small script '
                    'files totalling about 45 KB, all deferred. No '
                    'front-end framework.',
                    'Total blocking time measures 0 ms on a throttled '
                    'phone, so taps respond immediately.',
                    'Artwork is converted to WebP and capped at 512 px.',
                    'Static files are fingerprinted and pre-compressed so '
                    'browsers can cache them long-term.',
                ],
            },
            {
                'eyebrow': 'Mobile-First',
                'heading': 'Designed at phone width first',
                'points': [
                    'The stylesheets are written mobile-first: 40 '
                    'breakpoints that add layout as the screen grows, '
                    'against 4 that take it away.',
                    'Separate mobile menus for the public site and the '
                    'member area, announced to screen readers, closed with '
                    'Escape.',
                    'Layout shift while loading measures 0.03, well inside '
                    'Google’s 0.1 “good” line.',
                    'Every page has a matching phone-width screenshot '
                    'checked during development.',
                ],
            },
            {
                'eyebrow': 'SEO',
                'heading': 'Public pages found, private pages hidden',
                'intro': 'Lighthouse scores the homepage 100 for SEO on mobile.',
                'points': [
                    'Titles and descriptions Kate can set per page, with '
                    'sensible fallbacks so no page ever ships without one.',
                    'Structured data: Organization site-wide, Person on '
                    'Meet Kate, FAQPage on the Awakening quiz, and '
                    'breadcrumbs on every Guardian page.',
                    'A sitemap that lists members-first articles only once '
                    'they are public, so paid early access never leaks '
                    'into search.',
                    'Member areas, the admin and login pages send a header '
                    'telling search engines not to index them.',
                ],
            },
            {
                'eyebrow': 'Security',
                'heading': 'Members’ data treated as private by default',
                'points': [
                    'HTTPS with HSTS for a year, including subdomains and '
                    'preload, plus secure cookies and anti-framing headers.',
                    'Two-factor login required for every admin account, by '
                    'authenticator app or a single-use emailed link tied to '
                    'the browser that asked for it.',
                    'Card details go straight to Stripe; payment webhooks '
                    'are verified by signature and processed exactly once.',
                    'Evidence photos are stored privately and shown only to '
                    'the member who sent them or an authorized admin. '
                    'Anyone else gets a “not found”.',
                ],
            },
            {
                'eyebrow': 'Accessibility',
                'heading': 'Calm, readable, and usable with a keyboard',
                'points': [
                    'A skip-to-content link and visible focus outlines '
                    'throughout.',
                    'Motion switches off for visitors who ask their device '
                    'to reduce it.',
                    'Text contrast is measured (navy on ivory at 11.7:1), '
                    'and the gold accent is never used for text.',
                    'Decorative dragon art is hidden from screen readers.',
                ],
            },
        ],
    },

    # ─────────────────────────── Denis Law Group ───────────────────────────
    # Maintained, not built: describe the site as it stands.
    'denis-law-group': {
        'scorecard': {},
        'deep_dive': [
            {
                'eyebrow': 'The Site',
                'heading': 'A content-heavy legal site that has to stay current',
                'points': [
                    '25 practice-area pages covering divorce, custody, '
                    'child support, adoption, protective orders, estate '
                    'planning and more.',
                    'More than 100 published articles, organized into '
                    'custody, divorce, estate-planning and mediation '
                    'libraries.',
                    'An attorney profile, reviews page, consultation '
                    'request page, and a client login area.',
                ],
            },
            {
                'eyebrow': 'SEO',
                'heading': 'Search foundations on WordPress',
                'points': [
                    'A sitemap index split into pages, posts and '
                    'categories, kept up to date as content changes.',
                    'Organization, WebSite, WebPage and breadcrumb '
                    'structured data, plus canonical URLs and Open Graph '
                    'and Twitter preview tags.',
                    'Descriptive alt text on every homepage image.',
                    'Served over HTTPS on TLS 1.3, with plain-HTTP requests '
                    'redirected.',
                ],
            },
        ],
    },
}


# Narrative fields corrected in Sept 2026 (seed_case_studies uses these;
# migration 0073 applies them to existing rows only if unedited).
NARRATIVE = {
    'burgland-technologies': {
        'solution': 'A hand-coded Django build with no page builder and no CSS framework: six service pages and an Insights blog, every page carrying its own structured data, served over HTTPS on TLS 1.3 with headers that block framing and content-type sniffing, and clean semantic markup that stands up to someone opening developer tools.',
    },
    'food-trucks-of-san-antonio': {
        'solution': 'A Django and PostgreSQL platform rather than a static page: truck owners manage their own profiles and menus, a live map shows who is out right now, and every truck, cuisine and article has its own indexable page.\n\nCritical styles are written into the page, photos are resized on upload and served as WebP, and images further down wait until they are needed, so the first screen appears without the layout jumping around.',
        'results': 'A directory the community can actually use on a phone: 21 truck profiles with full menus (the homepage features the trucks that are active right now), a live map, reviews, and a way for local businesses to book a truck for an event, with every truck page structured for search.',
    },
    'moonieful-designs': {
        'challenge': 'A brand-clarity studio’s website is itself proof of the service. If the site looks templated or muddled, the promise of clarity falls flat before anyone reads a word.\n\nThe studio also needed more than a brochure: somewhere to run client projects, sell the founder’s book, and hand finished clients on to a website build without re-entering everything.',
        'summary': 'The website and client platform for a brand-clarity studio: a focused homepage, a shop for the founder’s book, and the portal the studio runs its client work through.',
        'solution': 'A deliberately quiet homepage that keeps attention on the studio’s message, with a direct path to a fit call rather than a buried contact page.\n\nBehind it, a custom Django platform: a client portal for projects, files and approvals, intake forms the founder builds herself, a Stripe-powered shop for her book, and a signed sync bridge that hands clients to Aspired Websites for their website build.',
        'results': 'A site that presents the studio without talking over it, a platform the studio runs on day to day, and an ongoing working relationship between the two studios.',
    },
}
