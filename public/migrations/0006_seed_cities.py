# Seeds the three existing location pages' content into City rows, then
# backfills CaseStudy.city from the free-text `location` field.
#
# This is what makes the location_san_antonio / location_atlanta /
# location_warner_robins hand-written templates collapsible into one
# generic view + template (public/views.py location_city) without losing
# any of their copy, meta tags, or schema — every string here is
# transcribed verbatim from the template it replaces.

from django.db import migrations


SAN_ANTONIO_HONESTY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>How This Actually Works</h3>
        <p>
            We’re based in Georgia and work with San Antonio clients
            remotely — calls, screen shares, and a staging link you can
            review from anywhere. Our <strong>(210)</strong> number is a real
            San Antonio line, not a redirect trick, and it’s where you
            reach us.
        </p>
    </article>

    <article class="card card--accent-left">
        <h3>Whether That Matters</h3>
        <p>
            For a website build, it usually doesn’t — the work is
            calls, drafts and reviews either way. If you specifically want
            someone who can sit in your office, hire locally, and we’ll
            say so on the call rather than talk you out of it.
        </p>
    </article>
</div>"""

SAN_ANTONIO_SECONDARY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>Law Firms</h3>
        <p>Our highest-value vertical, and our first San Antonio client. Practice-area architecture, security-hardened intake, and full ownership — see <a href="/services/web-design/law-firm-web-design/">law firm web design</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Local Service Businesses</h3>
        <p>Where one customer is worth more than the build, and being findable on a phone is the whole game. See <a href="/services/web-design/small-business-web-design/">small business web design</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Food, Events &amp; Hospitality</h3>
        <p>We built a citywide food truck directory here — mobile-first, fast on patchy data, and structured to keep growing as listings are added.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Anyone Stuck On A Platform</h3>
        <p>If you don’t own your site or can’t change it, moving is a specific job with specific risks. See <a href="/services/web-design/website-redesign/">website redesign</a>.</p>
    </article>
</div>"""

ATLANTA_HONESTY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>Where We Actually Are</h3>
        <p>
            Warner Robins, in Middle Georgia — about 100 miles
            south of Atlanta on I-75. Same state, same time zone, and
            close enough to sit in your office when a project genuinely
            calls for it. We don’t keep an Atlanta suite, and we
            won’t list one.
        </p>
    </article>

    <article class="card card--accent-left">
        <h3>How The Work Happens</h3>
        <p>
            Most of it remotely, because that’s what a website
            build actually is — calls, drafts, a staging link you
            review from anywhere. Kickoff or a launch review in person
            is a drive, not a flight, so ask if you want it.
        </p>
    </article>
</div>"""

ATLANTA_SECONDARY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>Law Firms</h3>
        <p>Our highest-value vertical. Atlanta legal search is genuinely competitive, so this is a long game rather than a quick win — practice-area architecture, security-hardened intake, full ownership. See <a href="/services/web-design/law-firm-web-design/">law firm web design</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Local Service Businesses</h3>
        <p>Where one customer is worth more than the build, and being findable on a phone is the whole game. See <a href="/services/web-design/small-business-web-design/">small business web design</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Anyone Stuck On A Platform</h3>
        <p>If you don’t own your site or can’t change it, moving is a specific job with specific risks. See <a href="/services/web-design/website-redesign/">website redesign</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Businesses Outside The Perimeter</h3>
        <p>Marietta, Alpharetta, Decatur, Sandy Springs — the metro is not one search market, and a site aimed at everywhere tends to rank nowhere. We build for the areas you actually serve.</p>
    </article>
</div>"""

ATLANTA_CROSS_LINK_HTML = """<p class="section-note">
    Closer to home? We’re based in
    <a href="/locations/warner-robins/">Warner Robins</a> and
    work across Middle Georgia. Outside Georgia entirely, our largest client
    base is in <a href="/locations/san-antonio/">San Antonio</a>.
</p>"""

ATLANTA_CTA_BODY_HTML = (
    'Call <a href="tel:+12108962536">(210) 896-2536</a> or book a time. '
    'We answer within 24 hours — and we’ll tell you honestly '
    'if you’d be better served by an agency in the city.'
)

WARNER_ROBINS_HONESTY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>You Can Meet Us</h3>
        <p>
            Warner Robins is home. Coffee in Houston County, a sit-down
            in Macon, or a drive out to Perry — that’s a
            normal week, not a special arrangement. Most of the build
            still happens over calls and a staging link, because
            that’s what works, but in person is genuinely on the
            table.
        </p>
    </article>

    <article class="card card--accent-left">
        <h3>We Work From Here, Not From A Storefront</h3>
        <p>
            There’s no walk-in office and we’re not going to
            invent one. Like most one-engineer studios we run as a
            service-area business — we come to you, or we work
            remotely, whichever suits the project.
        </p>
        <p>
            One thing worth explaining: our number is a
            <strong>210</strong> area code. That’s a San Antonio
            line from our largest client base, not a hint that
            we’re in Texas. It rings the same person, here in
            Warner Robins.
        </p>
    </article>
</div>"""

WARNER_ROBINS_SECONDARY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>To Be Found On A Phone, Nearby</h3>
        <p>
            Most people looking for a local business search on a phone
            and pick from what Google shows first. That means a fast
            site, correct business details, and a Google profile that
            matches them — see <a href="/services/seo/local-seo/">local SEO</a>.
        </p>
    </article>

    <article class="card card--accent-left">
        <h3>To Look Like A Real Operation</h3>
        <p>
            Robins Air Force Base anchors a lot of the local economy, and
            plenty of businesses here sell to people who compare two or
            three options before calling. A site that loads slowly or
            looks like a 2012 template loses that comparison.
        </p>
    </article>

    <article class="card card--accent-left">
        <h3>Not To Be Locked In</h3>
        <p>
            Some local agencies keep the site on their platform, so
            leaving means starting over. Ours is standard code on
            standard hosting — you own it and any developer can
            pick it up. See <a href="/pricing/">pricing</a>.
        </p>
    </article>

    <article class="card card--accent-left">
        <h3>Not To Overspend</h3>
        <p>
            If you need one page with your hours and a phone number,
            say so and we’ll tell you a $16/month builder is the
            better buy. Custom is worth it when the site has a job to do.
        </p>
    </article>
</div>"""

WARNER_ROBINS_CROSS_LINK_HTML = """<p class="section-note">
    Up the interstate? We work with
    <a href="/locations/atlanta/">Atlanta businesses</a> too
    — about 100 miles, and close enough to drive when a project needs
    it. Outside Georgia, most of our clients are in
    <a href="/locations/san-antonio/">San Antonio</a>.
</p>"""

WARNER_ROBINS_CTA_BODY_HTML = (
    'Call <a href="tel:+12108962536">(210) 896-2536</a> or book a time. '
    'We answer within 24 hours — and if a website isn’t what '
    'you need right now, we’ll say so.'
)

CITIES = [
    dict(
        name='San Antonio', slug='san-antonio', state='TX', sort_order=1,
        url_name='public:location_san_antonio',
        meta_title='Web Design San Antonio | Custom Websites | Aspired',
        meta_description=(
            'Custom web design for San Antonio businesses — three SA '
            'clients built and counting, including a law firm and a '
            'citywide food truck directory. Hand-coded, security-first, '
            'you own the code.'),
        og_title='San Antonio Web Design — Aspired Websites',
        og_description=(
            'Custom websites for San Antonio businesses. Three SA clients '
            'built. Hand-coded, security-first, and you own every file.'),
        hero_eyebrow='San Antonio',
        hero_heading_html=(
            'Web Design for <span class="accent">San Antonio</span> '
            'Businesses'),
        hero_lead=(
            'Custom, hand-coded websites for San Antonio businesses that '
            'need the site to actually bring in work. Three SA clients '
            'built so far — a law firm, a citywide food truck '
            'directory, and a design studio.'),
        honesty_eyebrow='Straight Up',
        honesty_heading='We Don’t Have a San Antonio Office',
        honesty_subheading=(
            'You’ll find agencies claiming addresses they don’t '
            'occupy. Here’s our actual setup.'),
        honesty_html=SAN_ANTONIO_HONESTY_HTML,
        proof_eyebrow='Proof',
        proof_heading='What We’ve Built in San Antonio',
        proof_subheading=(
            'Not a stock photo of the Riverwalk — actual clients you '
            'can go and look at.'),
        value_tiles_eyebrow='What You Get',
        value_tiles_heading='The Same Build, Wherever You Are',
        secondary_eyebrow='Who We Work With',
        secondary_heading='San Antonio Businesses We’re a Good Fit For',
        secondary_html=SAN_ANTONIO_SECONDARY_HTML,
        cross_link_html='',
        cta_heading='Let’s Talk About Your San Antonio Business',
        cta_body_html=(
            'Call <a href="tel:+12108962536">(210) 896-2536</a> or book a '
            'time. We answer within 24 hours — and we’ll tell '
            'you honestly if you’d be better served by someone '
            'local.'),
        schema_service_name='Web Design for San Antonio Businesses',
        schema_description=(
            'Custom, hand-coded web design and development for '
            'businesses in San Antonio, Texas. Delivered remotely from '
            'Georgia, with existing San Antonio clients across legal, '
            'food and events, and creative services.'),
        schema_area_served=[{'type': 'City', 'name': 'San Antonio'}],
    ),
    dict(
        name='Atlanta', slug='atlanta', state='GA', sort_order=2,
        url_name='public:location_atlanta',
        meta_title='Web Design Atlanta | Custom Websites | Aspired',
        meta_description=(
            'Custom web design for Atlanta businesses from a Georgia '
            'company — hand-coded, security-hardened, and you own '
            'every file. Based in Middle Georgia, on-site in Atlanta '
            'when it matters.'),
        og_title='Atlanta Web Design — Aspired Websites',
        og_description=(
            'Custom websites for Atlanta businesses, built by a Georgia '
            'company. Hand-coded, security-first, and you own the code.'),
        hero_eyebrow='Atlanta',
        hero_heading_html=(
            'Web Design for <span class="accent">Atlanta</span> '
            'Businesses'),
        hero_lead=(
            'Custom, hand-coded websites for Atlanta businesses that '
            'need the site to bring in work — not just exist. Built '
            'by a Georgia company, not an out-of-state agency that has '
            'never driven I-75.'),
        honesty_eyebrow='Straight Up',
        honesty_heading='We’re Georgia-Based, Not Atlanta-Based',
        honesty_subheading=(
            'Plenty of agencies rent a Buckhead mailbox and call it a '
            'headquarters. Here’s our actual setup.'),
        honesty_html=ATLANTA_HONESTY_HTML,
        proof_eyebrow='Proof',
        proof_heading='Work You Can Go And Look At',
        proof_subheading=(
            'Live sites, not mockups. We’ll be straight about where '
            'each client is.'),
        value_tiles_eyebrow='What You Get',
        value_tiles_heading='What Actually Ships',
        secondary_eyebrow='Who We Work With',
        secondary_heading='Atlanta Businesses We’re a Good Fit For',
        secondary_html=ATLANTA_SECONDARY_HTML,
        cross_link_html=ATLANTA_CROSS_LINK_HTML,
        cta_heading='Let’s Talk About Your Atlanta Business',
        cta_body_html=ATLANTA_CTA_BODY_HTML,
        schema_service_name='Web Design for Atlanta Businesses',
        schema_description=(
            'Custom, hand-coded web design and development for '
            'businesses in Atlanta, Georgia. Delivered by a '
            'Georgia-based studio in Warner Robins, remotely by default '
            'and on-site in Atlanta when a project calls for it.'),
        schema_area_served=[{'type': 'City', 'name': 'Atlanta'}],
    ),
    dict(
        name='Warner Robins', slug='warner-robins', state='GA',
        sort_order=3,
        url_name='public:location_warner_robins',
        meta_title='Web Design Warner Robins GA | Aspired Websites',
        meta_description=(
            'Custom web design in Warner Robins, Georgia — we’re '
            'actually based here. Hand-coded, security-hardened sites '
            'for Middle Georgia businesses, and you own every file.'),
        og_title='Warner Robins Web Design — Aspired Websites',
        og_description=(
            'Custom web design in Warner Robins, GA. Locally based, '
            'hand-coded, security-first, and you own the code.'),
        hero_eyebrow='Warner Robins, Georgia',
        hero_heading_html=(
            'Web Design in <span class="accent">Warner Robins</span>'),
        hero_lead=(
            'We’re not a national agency with a Georgia landing '
            'page — this is where we actually work. Custom, '
            'hand-coded websites for Warner Robins, Macon and Middle '
            'Georgia businesses.'),
        honesty_eyebrow='Actually Local',
        honesty_heading='Based Here, Not Just Targeting Here',
        honesty_subheading=(
            'The difference matters more than the marketing usually '
            'admits.'),
        honesty_html=WARNER_ROBINS_HONESTY_HTML,
        proof_eyebrow='Proof',
        proof_heading='Work You Can Go And Look At',
        proof_subheading='Live sites, not mockups.',
        value_tiles_eyebrow='',
        value_tiles_heading='',
        secondary_eyebrow='Middle Georgia',
        secondary_heading='What a Local Business Actually Needs',
        secondary_html=WARNER_ROBINS_SECONDARY_HTML,
        cross_link_html=WARNER_ROBINS_CROSS_LINK_HTML,
        cta_heading='Let’s Talk About Your Business',
        cta_body_html=WARNER_ROBINS_CTA_BODY_HTML,
        schema_service_name='Web Design for Warner Robins and Middle Georgia',
        schema_description=(
            'Custom, hand-coded web design and development for '
            'businesses in Warner Robins, Macon and Middle Georgia. '
            'Locally based, security-first, with full client ownership '
            'of the code.'),
        schema_area_served=[
            {'type': 'City', 'name': 'Warner Robins'},
            {'type': 'City', 'name': 'Macon'},
            {'type': 'City', 'name': 'Perry'},
            {'type': 'City', 'name': 'Centerville'},
            {'type': 'AdministrativeArea', 'name': 'Houston County, Georgia'},
        ],
    ),
]

# Priority order matters: a row matching "San Antonio" should never also
# match on a bare "GA"/"TX" substring, so the most specific city is
# checked first and matching stops at the first hit.
LOCATION_MATCHERS = [
    ('san-antonio', ['san antonio']),
    ('atlanta', ['atlanta']),
    ('warner-robins', ['warner robins', 'macon']),
]


def seed_cities_and_backfill(apps, schema_editor):
    City = apps.get_model('public', 'City')
    CaseStudy = apps.get_model('clients', 'CaseStudy')

    slug_to_city = {}
    for fields in CITIES:
        city, _ = City.objects.update_or_create(
            slug=fields['slug'], defaults=fields)
        slug_to_city[fields['slug']] = city

    unmatched = []
    for cs in CaseStudy.objects.all():
        location = (cs.location or '').lower()
        matched_slug = None
        for slug, needles in LOCATION_MATCHERS:
            if any(needle in location for needle in needles):
                matched_slug = slug
                break
        if matched_slug:
            cs.city = slug_to_city[matched_slug]
            cs.save(update_fields=['city'])
        elif location:
            unmatched.append((cs.title, cs.location))

    if unmatched:
        print(
            '\n  seed_cities: CaseStudy rows with a location that '
            'matched no City (left city=None, fix by hand in the admin):')
        for title, location in unmatched:
            print(f'    - {title!r} — location={location!r}')


def noop_reverse(apps, schema_editor):
    """Data migration — nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0005_remove_city_no_local_clients_note_html'),
        ('clients', '0062_casestudy_city_casestudy_is_hvac'),
    ]

    operations = [
        migrations.RunPython(seed_cities_and_backfill, noop_reverse),
    ]
