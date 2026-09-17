# "How Much Does Law Firm Web Design Cost?" pulls 220 impressions/month
# on "law firm website design cost" — real, commercial-intent traffic —
# but as written it sold law firm websites in first person ("Custom
# build — $2,500-$4,500 once — Ours") for a service no longer offered,
# at prices that no longer exist, on a site that says HVAC everywhere
# else. Rather than unpublish real traffic, the article is rewritten to
# stand as genuinely neutral, useful content: every price becomes a
# market observation instead of Aspired's price list, the three
# self-quotes (per-page addon, maintenance, hosting) are removed, and a
# short honest closing line explains why a law-firm article still lives
# on an HVAC site instead of pretending otherwise.
#
# Owner-approved two edits to the initial draft: the five-year
# comparison keeps a concrete number instead of going soft ("a fraction
# of the vendor total" -> "about $5,000 over five years"), and the
# custom-build floor moves from $2,500 to $3,000 so it doesn't overlap
# the freelancer tier's $2,500 ceiling.
#
# See public.tests_brand_consistency.LawFirmCostArticleNeutralityTests
# for the inverse-spec regression test: asserts the retired self-quote
# figures AND Aspired's current live prices are both absent, so a
# future price change can't coincidentally reintroduce a number here
# without being noticed.

from django.db import migrations


BODY = """<p>Law firm websites are priced differently from other small-business
sites, and not always for good reasons. Here is what the options
actually cost.</p>

<h2>The three ways firms buy a website</h2>

<h3>Vendor platforms — $200–$1,500/month, indefinitely</h3>
<p>The legal-marketing platforms. You pay monthly, forever, and in most
arrangements you do not own the site. Stop paying and it disappears —
along with the rankings you spent years earning. Over five years at
even $300/month that is $18,000, and you finish with nothing you can
take with you.</p>

<h3>General freelancer — $800–$2,500 once</h3>
<p>Cheapest up front. The risk is that legal has requirements a general
designer will not know to ask about: practice-area structure, bar
advertising rules, and intake that handles sensitive facts properly.</p>

<h3>Custom build — typically $3,000–$6,000 once</h3>
<p>The most durable option, if you find the right developer. You own
the code, the content and the domain, and the site keeps working
whether or not you keep paying anyone afterward. Price depends heavily
on scope: how many practice-area pages, how much custom functionality,
how much of the copy you are supplying versus having written. Extra
practice-area pages typically add to the quote rather than coming
free.</p>

<h2>The number that actually matters</h2>
<p>Not the invoice — the five-year total, and what you hold at the end
of it.</p>
<ul>
  <li><strong>Vendor at $300/month:</strong> $18,000, and you own
  nothing.</li>
  <li><strong>Custom at roughly $4,000 once, plus hosting at around
  $150–$300/year:</strong> about $5,000 over five years — and you own
  everything.</li>
</ul>
<p>Even adding ongoing maintenance for all five years, the custom route
finishes with an asset instead of a cancelled subscription.</p>

<h2>What drives a law firm quote up</h2>
<ul>
  <li><strong>Practice areas.</strong> The main driver. Each area needs
  a real page to rank for its own searches — a combined list ranks for
  none of them.</li>
  <li><strong>Attorney bios.</strong> Each attorney is effectively a
  page. Prospects search for people by name.</li>
  <li><strong>Intake.</strong> A form carrying facts about a legal
  problem before any engagement letter exists deserves proper
  handling — validated server-side, rate-limited, delivered straight to
  the firm.</li>
  <li><strong>Migration.</strong> Leaving a vendor means mapping every
  old URL so the rankings survive the move.</li>
</ul>

<h2>Questions to ask before you sign anything</h2>
<ol>
  <li>Do I own the source code?</li>
  <li>Who holds the domain registration? (Check this today — it is where
  switches get stuck.)</li>
  <li>What happens to the site if I stop paying?</li>
  <li>Can I take the content with me?</li>
  <li>What is the notice period?</li>
  <li>Is each practice area its own page, or one combined list?</li>
</ol>
<p>Question two catches more firms than the rest combined. Look up your
own domain's registration record before you do anything else.</p>

<h2>A note on bar advertising rules</h2>
<p>They vary by state and typically cover disclaimers, claims about
results, and how testimonials may be used. Build the site so required
disclaimers are easy to place and keep current. Confirming what your
state bar requires is the attorney's call — this is information, not
legal advice.</p>

<p><em>A note on who wrote this: Aspired Websites now builds for
<a href="/services/web-design/">HVAC contractors</a>, not law firms.
This article stays up because the math and the questions above are
still accurate no matter who ends up building the site.</em></p>"""


def rewrite_neutral(apps, schema_editor):
    Article = apps.get_model('public', 'Article')
    Article.objects.filter(
        slug='how-much-does-law-firm-web-design-cost',
    ).update(
        body=BODY,
        related_label='See what we build now →',
    )


def noop_reverse(apps, schema_editor):
    """Data migration — nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0009_custom_website_cost_live_pricing'),
    ]

    operations = [
        migrations.RunPython(rewrite_neutral, noop_reverse),
    ]
