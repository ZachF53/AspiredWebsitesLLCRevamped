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

T-Bear Tool & Equipment Rental was added 2026-10-17 and checked against
the live site that day: the catalog and its 15 machines, the seven job
pages and seven category filters, the published rental terms and
requirements, the response headers, and the platform the site runs on
(WordPress with WooCommerce and a rental/booking layer). It is a
`redesigned` engagement on WordPress, NOT a hand-coded build, so nothing
in its section claims hand-coded markup, a content security policy, or
the Django platform work the other studies describe. The operations
layer behind it (calls, texts, agreements, review follow-up) is
described by what it does; the vendor is deliberately not named, per
owner direction.

T-Bear has no scorecard: no Lighthouse run, uptime history or security
scan of our own exists for it yet, and §15 forbids estimating one.

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
                'heading': 'The system the studio runs on',
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
                    'the searches people run (“Curbside Eats | '
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

    # ──────────────── T-Bear Tool & Equipment Rental ────────────────
    # Redesigned on WordPress, not hand-coded. See the module docstring.
    't-bear-tool-equipment-rental': {
        'scorecard': {},
        'deep_dive': [
            {
                'eyebrow': 'The Storefront',
                'heading': 'Fifteen machines, each with a page and a price',
                'intro': (
                    'The fleet is a real catalog rather than a photo gallery. '
                    'Every machine has its own listing, and the reservation '
                    'form knows which machine it is for.'),
                'points': [
                    'Fifteen machines listed by make and model, each with the '
                    'nickname the business actually calls it: Sir Suds the '
                    'Simpson pressure washer, Brutus the Bosch jackhammer, '
                    'Rex the Husqvarna concrete saw, Hustler the zero-turn '
                    'mower.',
                    'Every listing publishes its daily rate, so nobody has to '
                    'send a message to find out what something costs.',
                    'Engine and power specifications sit on the listing, so a '
                    'customer can confirm a machine will do the job before '
                    'they request it.',
                    'Seven filters group the fleet by the kind of work: '
                    'cleaning, concrete and demo, cutting, demolition, lawn, '
                    'lawn and landscape, and site prep.',
                    'The site also carries T-Bear’s own pressure washing and '
                    'lawn care services, for customers who would rather hire '
                    'the job out than rent the machine.',
                ],
            },
            {
                'eyebrow': 'Start With The Job',
                'heading': 'Browsing by the work, not the tool name',
                'intro': (
                    'Most customers know what they are trying to get done, '
                    'not which machine does it.'),
                'points': [
                    'Seven job pages act as the front door: clean a driveway, '
                    'break or remove concrete, install pavers, compact gravel '
                    'or soil, cut concrete or metal, clear limbs or small '
                    'trees, and mow an overgrown lot.',
                    'Each one names the machine for that job and links '
                    'straight to its reservation, so the path from problem to '
                    'request is two clicks.',
                    'Customers who already know what they want can skip it '
                    'and go straight to the catalog.',
                    'Reserving online takes no payment. The dates and the '
                    'price are confirmed first, and the customer pays at '
                    'pickup or delivery.',
                ],
            },
            {
                'eyebrow': 'Automated Communication',
                'heading': 'The reservation tells the customer where it stands',
                'intro': (
                    'The work that used to be a string of Facebook replies is '
                    'now a sequence the system runs on its own.'),
                'points': [
                    'A reservation moves through confirmation, rental '
                    'agreement, pickup, return and review, with a message '
                    'going out at the steps that need one.',
                    'Agreement links are sent by text and email once the '
                    'dates and price are confirmed.',
                    'The team is alerted on the reservation activity that '
                    'needs a person, so follow-up does not depend on someone '
                    'remembering to check an inbox.',
                    'A published messaging terms page covers consent for the '
                    'text messages, alongside the site’s privacy policy.',
                ],
            },
            {
                'eyebrow': 'Rental Agreements',
                'heading': 'Signed before the machine leaves',
                'intro': (
                    'Digital rental agreements are issued through Firma.dev '
                    'once a reservation is confirmed.'),
                'points': [
                    'The customer signs from their phone instead of on a '
                    'clipboard at the tailgate.',
                    'The site tracks every agreement as pending or signed, so '
                    'the state of each reservation is visible without anyone '
                    'chasing paperwork.',
                    'The full terms are published on the site rather than '
                    'handed over at pickup: who can rent, deposits, payment, '
                    'fuel and cleaning, damage and loss, late returns, '
                    'cancellations, delivery, and liability.',
                    'Requirements are deliberately short and stated up front: '
                    'a valid photo ID, a card in the renter’s own name, and '
                    'being 18 or older. No business license, no proof of '
                    'insurance, no account application.',
                    'Deposits are a card hold released on return, quoted '
                    'before the reservation is made, so there is no surprise '
                    'at the counter.',
                ],
            },
            {
                'eyebrow': 'Reviews',
                'heading': 'Asking at the moment the job is still fresh',
                'points': [
                    'The review request is the last stage of the reservation, '
                    'so it goes out after the machine comes back rather than '
                    'weeks later.',
                    'Follow-up runs on its own, so a good rental can become a '
                    'public review without the owner having to ask in person.',
                    'Because it is tied to the reservation and not to a '
                    'manual list, every completed rental gets the same ask.',
                ],
            },
            {
                'eyebrow': 'Bear, On The Phone',
                'heading': 'An AI receptionist that screens the junk',
                'intro': (
                    'Bear is T-Bear’s AI voice receptionist, and it answers '
                    'first so the owner does not have to stop work for every '
                    'ring.'),
                'points': [
                    'Bear answers the common rental questions and explains how '
                    'the request process works.',
                    'Likely spam and robocalls are screened at the first '
                    'layer instead of ringing through.',
                    'A caller who genuinely needs the owner is transferred '
                    'straight to him. There is no phone tree to navigate.',
                ],
            },
            {
                'eyebrow': 'Bear, On The Site',
                'heading': 'The same assistant, in the corner of the page',
                'points': [
                    'A chat assistant on every page answers questions about '
                    'equipment, starting rates, pickup versus delivery, '
                    'hours, and how renting works.',
                    'Answers are specific to T-Bear rather than generic, and '
                    'point visitors toward the right machine or the '
                    'reservation form.',
                    'Website chat is kept separate from the business text '
                    'inbox, so a browsing question never lands in the same '
                    'queue as a live rental.',
                ],
            },
            {
                'eyebrow': 'The Platform',
                'heading': 'WordPress, set up to actually take reservations',
                'intro': (
                    'T-Bear is a WordPress site, not a hand-coded build. What '
                    'changed is that the rental side is real functionality '
                    'instead of a contact form next to some photos.'),
                'points': [
                    'The catalog runs on WooCommerce with a rental and '
                    'booking layer on top, so dates and availability belong '
                    'to the machine rather than to a message thread.',
                    'Served over HTTPS with strict transport security, on '
                    'managed hosting with page caching and a CDN in front '
                    'of it.',
                    'Titles, meta descriptions and structured data are '
                    'managed deliberately rather than left to the theme '
                    'defaults.',
                    'The operations layer sits alongside the site and handles '
                    'the calls, texts, agreements and review follow-up. The '
                    'website is the storefront; that layer is the back '
                    'office.',
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
        'results': 'A directory the community can use on a phone: 21 truck profiles with full menus (the homepage features the trucks that are active right now), a live map, reviews, and a way for local businesses to book a truck for an event, with every truck page structured for search.',
    },
    'moonieful-designs': {
        'challenge': 'A brand-clarity studio’s website is itself proof of the service. If the site looks templated or muddled, the promise of clarity falls flat before anyone reads a word.\n\nThe studio also needed more than a brochure: somewhere to run client projects, sell the founder’s book, and hand finished clients on to a website build without re-entering everything.',
        'summary': 'The website and client platform for a brand-clarity studio: a focused homepage, a shop for the founder’s book, and the portal the studio runs on.',
        'solution': 'A deliberately quiet homepage that keeps attention on the studio’s message, with a direct path to a fit call rather than a buried contact page.\n\nBehind it, a custom Django platform: a client portal for projects, files and approvals, intake forms the founder builds herself, a Stripe-powered shop for her book, and a signed sync bridge that hands clients to Aspired Websites for their website build.',
        'results': 'A site that presents the studio without talking over it, a platform the studio runs on day to day, and an ongoing working relationship between the two studios.',
    },
}
