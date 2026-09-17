# Sept 2026 repositioning, FIX 4 (post-pivot cleanup) — the three
# location pages survived the City refactor with their URLs intact, but
# their body copy was still industry-generic ("What a Local Business
# Actually Needs" could be any vertical). This rewrites the copy to
# speak to HVAC contractors specifically: seasonal demand, emergency
# calls, how homeowners search for AC repair, local competition, and
# Google reviews driving the map pack.
#
# Deliberately NOT touched: honesty_eyebrow/heading/subheading/html on
# all three rows. That's the "we don't have a walk-in office" section,
# and on Warner Robins it also carries the 210-area-code explanation —
# both were explicitly called out to keep verbatim. cross_link_html and
# cta_heading/cta_body_html are geography, not industry, so they stay
# too. value_tiles_heading/eyebrow are untouched because the six value
# tiles themselves are hardcoded in location_city.html, not sourced
# from these fields, and were already industry-neutral.
#
# secondary_html link targets point at live pages (/pricing/,
# /services/review-automation/) rather than the retired specialist
# pages the old copy linked to — those now 301 through service_web_design,
# and a freshly-written internal link shouldn't route through a redirect
# when a direct target exists.

from django.db import migrations


SAN_ANTONIO_SECONDARY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>Residential AC &amp; Heating Repair</h3>
        <p>The bread and butter call: a unit fails in July heat and the homeowner is searching on a phone, right now, for whoever shows up first. See <a href="/services/review-automation/">automated review generation</a> for how we help you win the map pack.</p>
    </article>

    <article class="card card--accent-left">
        <h3>24/7 Emergency Service Companies</h3>
        <p>If you run emergency dispatch, the site's whole job is to make "yes, we're open now" the first thing a visitor sees. Buried contact info costs you the call.</p>
    </article>

    <article class="card card--accent-left">
        <h3>New Construction &amp; Commercial HVAC</h3>
        <p>A different buyer and a longer sales cycle. The site needs to establish credibility with a builder or property manager, not just answer a panic call.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Multi-Truck Operations</h3>
        <p>Once you're running more than one truck, the site is doing real work: routing the right lead to the right service. See <a href="/pricing/">pricing</a>.</p>
    </article>
</div>"""

ATLANTA_SECONDARY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>Residential AC &amp; Heating Repair</h3>
        <p>Georgia summers are long and humid, and an outage turns into an emergency fast. Atlanta's residential HVAC market is crowded with national franchises, so the site has to work as hard as the truck does. See <a href="/services/review-automation/">automated review generation</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>24/7 Emergency Service Companies</h3>
        <p>The site's job is to make "yes, we're open now" obvious in the first five seconds, not something a visitor scrolls to find.</p>
    </article>

    <article class="card card--accent-left">
        <h3>New Construction &amp; Commercial HVAC</h3>
        <p>Builders and property managers comparing bids need a site that reads as an established operation, not a one-page flyer.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Businesses Outside The Perimeter</h3>
        <p>Marietta, Alpharetta, Decatur, Sandy Springs. HVAC search is hyper-local, and a site built for "Atlanta" in general tends to rank for nowhere in particular. We build for the areas you actually serve.</p>
    </article>
</div>"""

WARNER_ROBINS_SECONDARY_HTML = """<div class="cards cards--2col">
    <article class="card card--accent-left">
        <h3>To Be Found On A Phone, In An Emergency</h3>
        <p>Most HVAC searches happen from a phone at the worst possible moment: a unit down in Middle Georgia humidity. A slow site loses that call before it starts.</p>
    </article>

    <article class="card card--accent-left">
        <h3>To Win The Map Pack, Not Just The Search Bar</h3>
        <p>Robins Air Force Base anchors a lot of the local economy, and a lot of residential HVAC demand comes from the neighborhoods built up around it. Reviews and a correct Google profile matter as much as the site itself. See <a href="/services/review-automation/">automated review generation</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Not To Be Locked In</h3>
        <p>Some local shops build on a platform they don't own, so leaving means starting over. Ours is standard code on standard hosting, and you own it, so any developer can pick it up. See <a href="/pricing/">pricing</a>.</p>
    </article>

    <article class="card card--accent-left">
        <h3>Not To Overspend</h3>
        <p>If you need one page with your hours and a phone number, say so and we'll tell you a $16/month builder is the better buy. Custom is worth it once the site has real dispatch and reputation work to do.</p>
    </article>
</div>"""


CITY_UPDATES = {
    'san-antonio': dict(
        meta_title='HVAC Website Design San Antonio | Aspired Websites',
        meta_description=(
            'Custom websites for San Antonio HVAC contractors, built for '
            'emergency calls, seasonal demand and the Google map pack. '
            'Hand-coded, security-first, you own the code.'),
        og_title='San Antonio HVAC Website Design | Aspired Websites',
        og_description=(
            'Custom websites for San Antonio HVAC contractors, built to '
            'turn an "AC is out" search into a phone call. Hand-coded, '
            'security-first, and you own every file.'),
        hero_heading_html=(
            'Web Design for <span class="accent">San Antonio</span> '
            'HVAC Contractors'),
        hero_lead=(
            'South Texas summers push AC systems past their limit every '
            'year, and when a unit fails, homeowners are searching for '
            'whoever can be there today. A site that loads slowly or '
            'buries your phone number loses that call to the contractor '
            'whose site comes up first. We build sites for that moment: '
            'fast on a phone, the number impossible to miss, and '
            'structured so the map pack has a reason to show you.'),
        secondary_heading='San Antonio HVAC Companies We’re a Good Fit For',
        secondary_html=SAN_ANTONIO_SECONDARY_HTML,
        schema_service_name='Web Design for San Antonio HVAC Contractors',
        schema_description=(
            'Custom, hand-coded web design and development for HVAC '
            'contractors in San Antonio, Texas. Built for emergency-call '
            'urgency and the local map pack that drives most residential '
            'HVAC searches. Delivered remotely from Georgia.'),
    ),
    'atlanta': dict(
        meta_title='HVAC Website Design Atlanta | Aspired Websites',
        meta_description=(
            'Custom web design for Atlanta HVAC contractors from a '
            'Georgia company. Built for emergency-call urgency and local '
            'search. Hand-coded, security-hardened, you own every file.'),
        og_title='Atlanta HVAC Website Design | Aspired Websites',
        og_description=(
            'Custom websites for Atlanta HVAC contractors, built by a '
            'Georgia company. Hand-coded, security-first, and you own '
            'the code.'),
        hero_heading_html=(
            'Web Design for <span class="accent">Atlanta</span> '
            'HVAC Contractors'),
        hero_lead=(
            'Georgia summers are long and humid, and an AC outage turns '
            'into an emergency fast. Atlanta’s HVAC market is '
            'crowded, from national franchises to independent shops, so '
            'the contractors who win the call are the ones whose site '
            'loads fast, shows up in the map pack, and makes it obvious '
            'they can come out today. That’s what we build.'),
        secondary_heading='Atlanta HVAC Companies We’re a Good Fit For',
        secondary_html=ATLANTA_SECONDARY_HTML,
        schema_service_name='Web Design for Atlanta HVAC Contractors',
        schema_description=(
            'Custom, hand-coded web design and development for HVAC '
            'contractors in Atlanta, Georgia. Built for emergency-call '
            'urgency and the map-pack search behavior that drives '
            'residential HVAC leads. Delivered by a Georgia-based '
            'studio in Warner Robins.'),
    ),
    'warner-robins': dict(
        meta_title='HVAC Website Design Warner Robins GA | Aspired Websites',
        meta_description=(
            'Custom web design for HVAC contractors in Warner Robins, '
            'Georgia. We’re actually based here. Hand-coded, '
            'security-hardened sites built for emergency-call urgency, '
            'and you own every file.'),
        og_title='Warner Robins HVAC Website Design | Aspired Websites',
        og_description=(
            'Custom web design for HVAC contractors in Warner Robins, '
            'GA. Locally based, hand-coded, security-first, and you own '
            'the code.'),
        hero_heading_html=(
            'HVAC Web Design in '
            '<span class="accent">Warner Robins</span>'),
        hero_lead=(
            'Middle Georgia heat and humidity keep AC units working '
            'overtime, and when one fails, homeowners in Houston County '
            'are searching for whoever can get there fastest. We build '
            'sites for Warner Robins, Macon and Middle Georgia HVAC '
            'contractors: your number is impossible to miss, the site '
            'loads fast on a phone in July, and the structure gives the '
            'map pack a reason to show you first.'),
        secondary_heading='What A Middle Georgia HVAC Company Actually Needs',
        secondary_html=WARNER_ROBINS_SECONDARY_HTML,
        schema_service_name=(
            'Web Design for Warner Robins and Middle Georgia HVAC '
            'Contractors'),
        schema_description=(
            'Custom, hand-coded web design and development for HVAC '
            'contractors in Warner Robins, Macon and Middle Georgia. '
            'Locally based, built for emergency-call urgency, with full '
            'client ownership of the code.'),
    ),
}


def rewrite_city_copy(apps, schema_editor):
    City = apps.get_model('public', 'City')
    for slug, fields in CITY_UPDATES.items():
        City.objects.filter(slug=slug).update(**fields)


def noop_reverse(apps, schema_editor):
    """Data migration — nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0006_seed_cities'),
    ]

    operations = [
        migrations.RunPython(rewrite_city_copy, noop_reverse),
    ]
