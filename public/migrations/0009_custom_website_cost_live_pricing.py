# "How Much Does a Custom Website Cost?" quoted retired pricing
# ($2,500-$4,500 build, $299/mo maintenance, $150/yr hosting) — it
# predates the Sept 2026 HVAC repositioning and was never updated when
# ServiceTier's hvac-* rows replaced the general build/maintenance
# tiers. It gets ~121 impressions/month in Search Console, so the wrong
# numbers were live on one of the best-performing pages on the site.
#
# Article.body is raw stored HTML rendered with `{{ article.body|safe }}`
# (public/templates/public/insight_detail.html) — it never passes
# through the Django template engine, so it can't invoke a `{% price %}`
# tag the way public/templates/public/*.html can. Numbers are hardcoded
# here as a result; public/tests_brand_consistency.py's
# CustomWebsiteCostPriceConsistencyTests asserts each one against the
# live ServiceTier rows so a future price change in the pricing admin
# fails this test instead of silently going stale again.

from django.db import migrations


BODY = """<p>Nobody publishes this number, which is why you are reading a fourth
article about it. So: <strong>a custom, hand-coded website for an HVAC
company costs $2,000 upfront, or $105 a month for 24 months</strong>
($2,520 total, financed). Hosting and security runs $45 a month on its
own, or bundled into the Full Plan below.</p>

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

<h3>$2,000 — custom build</h3>
<p>Where we sit. Flat price, no range: $2,000 upfront, or $105 a month
for 24 months if you would rather spread it out. Every page designed
around your business and written from scratch. No theme to fight, no
plugin to break, and the structure can be built around how homeowners
actually search when a unit fails.</p>

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
  <li><strong>Hosting and security</strong> — $45/month on its own, or
  bundled into the Full Plan.</li>
  <li><strong>The Full Plan</strong> — $250/month, build payment
  included, covering hosting, maintenance, unlimited content updates
  and automated review generation. Drops to $145/month automatically
  once the build is paid off at month 24. If you paid the build
  upfront, the same plan is $145/month from day one.</li>
  <li><strong>Out-of-scope work</strong> — $85/hour, invoiced before it
  starts, for anything outside the original build.</li>
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


def update_pricing(apps, schema_editor):
    Article = apps.get_model('public', 'Article')
    Article.objects.filter(
        slug='how-much-does-a-custom-website-cost').update(body=BODY)


def noop_reverse(apps, schema_editor):
    """Data migration — nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0008_insights_dead_link_cleanup'),
    ]

    operations = [
        migrations.RunPython(update_pricing, noop_reverse),
    ]
