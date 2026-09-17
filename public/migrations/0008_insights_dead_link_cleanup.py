# Same bug class as public/migrations/0007_city_hvac_copy.py and the
# service_web_design.html cleanup: internal links to pages that now 301
# through _RETIRED_TO_WEB_DESIGN (public/urls.py). Found while auditing
# the two location_city.html value-tile links, once the search widened
# to "every internal link to a retired URL" rather than just the two
# already known — these three published /insights/ articles had five
# more between them, including two article CTA buttons.
#
# how-much-does-a-custom-website-cost: the "Migration" bullet's link to
# the retired website-redesign page is dropped (no replacement offering
# exists); the "SEO" bullet's link to the retired local-seo page is
# repointed to review-automation and reworded, since that's what
# actually drives map-pack performance now.
#
# why-your-business-isnt-showing-up-on-google: the "law firm SEO" link
# in section 5 is dropped and the sentence rewritten HVAC-specific
# (the rest of the article is already industry-neutral troubleshooting
# and stays as-is). The article's own bottom CTA pointed at the same
# retired local-seo page and is repointed to review-automation.
#
# how-much-does-law-firm-web-design-cost: ONLY the CTA is repointed
# here, to the general web-design page. The body is still wholly
# law-firm-positioned with pre-pivot pricing throughout — that's a
# separate decision (rewrite/unpublish/leave as historical) still
# pending, not something this migration resolves.

from django.db import migrations


CUSTOM_WEBSITE_COST_BODY = """<p>Nobody publishes this number, which is why you are reading a fourth
article about it. So: <strong>a custom, hand-coded website costs
$2,500 to $4,500</strong> for most small businesses and law firms.
Hosting is $150 a year. Optional maintenance starts at $299 a month.</p>

<p>That is our pricing, published, not a range designed to get you on a
call. What follows is what actually moves the number, and where the
cheaper options genuinely beat us.</p>

<h2>The four price tiers, honestly</h2>

<h3>$0–$50/month — website builders</h3>
<p>Wix, Squarespace, GoDaddy. You do the work. Genuinely fine for a
single page with your hours and phone number, and we will tell you so
rather than sell against it. The costs show up later: you cannot change
what the platform will not let you change, and the site is rented, not
owned.</p>

<h3>$500–$1,500 — freelancer on a template</h3>
<p>A person installs a theme and fills it with your content. Faster and
cheaper, and the result is only as good as the theme. The common
failure is that it looks identical to three competitors who bought the
same one.</p>

<h3>$2,500–$4,500 — custom build</h3>
<p>Where we sit. Every page designed around your business and written
from scratch. No theme to fight, no plugin to break, and the structure
can be built around how people actually search for what you do.</p>

<h3>$10,000+ — agency</h3>
<p>A team, a strategist, a project manager, and an office to pay for.
Sometimes worth it. Often you are buying the overhead.</p>

<h2>What actually changes the price</h2>
<p>Four things, in order of impact:</p>
<ul>
  <li><strong>Page count.</strong> A five-page site is not half the work
  of a ten-page site, but it is not the same either. For law firms this
  is usually practice areas — each one needs its own real page.</li>
  <li><strong>Functionality.</strong> Booking, payments, client logins,
  intake forms that route somewhere specific. A brochure site and a site
  that <em>does</em> something are different projects.</li>
  <li><strong>Content.</strong> If you have copy, we build. If you do
  not, we write it — that is real work and it is the stage that most
  often slips.</li>
  <li><strong>Migration.</strong> Moving an existing site means mapping
  every old URL so you do not lose the rankings you already have. That
  is real work, and it matters more than the design.</li>
</ul>

<h2>The costs people forget</h2>
<p>The build is not the whole number. Budget for:</p>
<ul>
  <li><strong>Domain</strong> — $12–$20/year, and you should own it, not
  your developer.</li>
  <li><strong>Hosting</strong> — $150/year with us.</li>
  <li><strong>Maintenance</strong> — optional, $299/month and up. Updates,
  monitoring, backups, support. If you skip it, the site keeps working;
  you are just on your own.</li>
  <li><strong>SEO</strong> — a separate ongoing discipline. A well-built
  site can be found. Being found <em>first</em>, especially in the map
  pack, comes from reviews as much as it does from the build. See
  <a href="/services/review-automation/">automated review generation</a>.</li>
</ul>

<h2>A decision framework</h2>
<p>Spend the money on custom when at least two of these are true:</p>
<ol>
  <li>One customer is worth more than the build cost.</li>
  <li>You compete on search, and your competitors all use the same theme.</li>
  <li>The site has a job beyond existing — booking, intake, payments.</li>
  <li>You are losing mobile visitors to load time today.</li>
  <li>You want to own the asset rather than rent it indefinitely.</li>
</ol>
<p>If none of those are true, buy a $16/month builder subscription and
spend the difference on something that grows the business faster. That
advice costs us work and it is still the right advice.</p>

<h2>Why we publish the number</h2>
<p>Quoting "it depends" is a way of finding out what you will pay before
saying what it costs. You can see our prices without talking to anyone,
and if the range does not fit, you have lost nothing but a minute.</p>"""

GOOGLE_VISIBILITY_BODY = """<p>You searched for your own business, did not find it, and now you are
here. Below are the seven causes, roughly in the order they turn out to
be the real one — with how to check each yourself.</p>

<h2>1. You are searching in a way nobody else does</h2>
<p>Searching your exact business name while logged in, from your own
office, on the network you always use, tells you almost nothing. Google
personalises heavily. Search the way a <em>customer</em> would — the
service plus the city — in a private window.</p>
<p><strong>Fix:</strong> none needed. Re-test properly first, before
spending money on a problem you might not have.</p>

<h2>2. You have no Google Business Profile, or it is half-built</h2>
<p>For local searches, the map block above the normal results is where
most clicks go. If you are not in it, you are effectively invisible
regardless of how good your website is. Most profiles have a name, an
address, and nothing else.</p>
<p><strong>Fix:</strong> claim it, then actually fill it in — correct
primary category, services, hours, real photos, service areas.
<strong>Timeline:</strong> weeks, and the fastest win available.</p>

<h2>3. Your site is new</h2>
<p>New sites are not trusted immediately. Three to six months before
meaningful movement is normal, longer in competitive markets.</p>
<p><strong>Fix:</strong> patience, plus doing the other six properly
while you wait.</p>

<h2>4. Google cannot read your site properly</h2>
<p>Missing title tags, no headings, JavaScript that hides the content
from crawlers, pages blocked in robots.txt, or no sitemap. More common
than people expect, especially on builder and page-builder sites.</p>
<p><strong>Fix:</strong> a technical audit. Our
<a href="/audit/">free website audit</a> checks the basics in about a
minute. <strong>Timeline:</strong> fixes land in days; Google notices
in weeks.</p>

<h2>5. One page is trying to be ten</h2>
<p>The single biggest structural mistake. A "Services" page listing
eight things cannot be the best answer for eight different searches.
Google picks whoever wrote a whole page about the one thing.</p>
<p><strong>Fix:</strong> a real page per service. For an HVAC company
that usually means AC repair, heating repair, installation and
maintenance plans, each getting its own page instead of one combined
list. <strong>Timeline:</strong> two to three months to see movement.</p>

<h2>6. Your site is too slow</h2>
<p>Speed is both a ranking factor and a conversion factor. If your
homepage takes more than about three seconds on mobile data, you are
losing people before they see anything — and Google knows.</p>
<p><strong>Fix:</strong> usually images and bloat. When we rebuilt this
site, mobile load time went from 4.1 seconds to 1.5 — measured before
and after, not estimated.</p>

<h2>7. Your listings disagree with each other</h2>
<p>An old suite number on one directory, a former phone number on
another. Google uses consistency as a confidence signal, and
inconsistency quietly suppresses you.</p>
<p><strong>Fix:</strong> pick one exact format and make every listing
match it. Tedious, unglamorous, effective.</p>

<h2>The order to work through it</h2>
<ol>
  <li>Re-test properly in a private window.</li>
  <li>Fix your Google Business Profile — fastest return.</li>
  <li>Run a technical audit and fix what it finds.</li>
  <li>Fix your speed if it is bad.</li>
  <li>Split services onto real pages.</li>
  <li>Clean up listings.</li>
  <li>Wait. It takes months, and anyone who says otherwise is selling.</li>
</ol>

<p>One thing worth saying plainly: nobody can guarantee you a ranking.
Anyone who does is either targeting keywords nobody searches or is not
being straight with you.</p>"""


ARTICLE_UPDATES = {
    'how-much-does-a-custom-website-cost': dict(
        body=CUSTOM_WEBSITE_COST_BODY,
    ),
    'why-your-business-isnt-showing-up-on-google': dict(
        body=GOOGLE_VISIBILITY_BODY,
        related_url='/services/review-automation/',
        related_label='See automated review generation →',
    ),
    'how-much-does-law-firm-web-design-cost': dict(
        related_url='/services/web-design/',
        related_label='See our web design services →',
    ),
}


def fix_dead_links(apps, schema_editor):
    Article = apps.get_model('public', 'Article')
    for slug, fields in ARTICLE_UPDATES.items():
        Article.objects.filter(slug=slug).update(**fields)


def noop_reverse(apps, schema_editor):
    """Data migration — nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0007_city_hvac_copy'),
    ]

    operations = [
        migrations.RunPython(fix_dead_links, noop_reverse),
    ]
