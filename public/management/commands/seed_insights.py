"""
Seed the first /insights/ articles (Master Plan §12).

Order follows the measured demand in KEYWORD_RESEARCH_FINDINGS.md §7,
not the plan's original guess:

  1. How Much Does a Custom Website Cost?   ~5,060/mo cluster, stable
  2. Why Your Business Isn't Showing Up on Google      480/mo, stable
  3. How Much Does Law Firm Web Design Cost?  10/mo direct, top ticket

Idempotent — safe on every deploy. `--force` overwrites, the default
never clobbers edits made in the admin.

Every article carries what §12's quality gate demands: a decision
framework or real numbers, and an internal link back to the
commercial page it supports. Prices quoted are the real published
prices from the pricing page — nothing invented.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from public.models import Article

ARTICLES = [
    {
        'slug': 'how-much-does-a-custom-website-cost',
        'title': 'How Much Does a Custom Website Cost?',
        'summary': (
            'Real numbers, what drives them up or down, and an honest '
            'answer about when you should spend far less than we charge.'
        ),
        'related_url': '/pricing/',
        'related_label': 'See our full pricing →',
        'body': """
<p>Nobody publishes this number, which is why you are reading a fourth
article about it. So: <strong>a custom, hand-coded website for an HVAC
company costs $2,000 upfront, or $105 a month for 24 months</strong>
($2,520 total, financed). Hosting and security runs $45 a month on its
own, or bundled into the Full Plan below.</p>

<p>That is our pricing, published, not a range designed to get you on a
call. What follows is what actually moves the number, and where the
cheaper options genuinely beat us.</p>

<h2>The four price tiers, honestly</h2>

<h3>$0–$50/month: website builders</h3>
<p>Wix, Squarespace, GoDaddy. You do the work. Genuinely fine for a
single page with your hours and phone number, and we will tell you so
rather than sell against it. The costs show up later: you cannot change
what the platform will not let you change, and the site is rented, not
owned.</p>

<h3>$500–$1,500: freelancer on a template</h3>
<p>A person installs a theme and fills it with your content. Faster and
cheaper, and the result is only as good as the theme. The common
failure is that it looks identical to three competitors who bought the
same one.</p>

<h3>$2,000: custom build</h3>
<p>Where we sit. Flat price, no range: $2,000 upfront, or $105 a month
for 24 months if you would rather spread it out. Every page designed
around your business and written from scratch. No theme to fight, no
plugin to break, and the structure can be built around how homeowners
actually search when a unit fails.</p>

<h3>$10,000+: agency</h3>
<p>A team, a strategist, a project manager, and an office to pay for.
Sometimes worth it. Often you are buying the overhead.</p>

<h2>What changes the <em>work</em>, and when we’ll tell you the flat price doesn’t fit</h2>
<p>The build is one flat price. These four things change how much work it is.
If yours needs far more than a normal build, we’ll say so on the call and
quote it rather than guess:</p>
<ul>
  <li><strong>Page count.</strong> A five-page site is not half the work
  of a ten-page site, but it is not the same either. For an HVAC or home-service company this
  is usually service pages and service-area pages: each one needs its own real page.</li>
  <li><strong>Functionality.</strong> Booking, payments, client logins,
  intake forms that route somewhere specific. A brochure site and a site
  that <em>does</em> something are different projects.</li>
  <li><strong>Content.</strong> If you have copy, we build. If you do
  not, we write it. That is real work, and it is the stage that most
  often slips.</li>
  <li><strong>Migration.</strong> Moving an existing site means mapping
  every old URL so you do not lose the rankings you already have. That
  is real work, and it matters more than the design.</li>
</ul>

<h2>The costs people forget</h2>
<p>The build is not the whole number. Budget for:</p>
<ul>
  <li><strong>Domain.</strong> $12–$20/year, and you should own it, not
  your developer.</li>
  <li><strong>Hosting and security.</strong> $45/month on its own, or
  bundled into the Full Plan.</li>
  <li><strong>The Full Plan.</strong> $250/month, build payment
  included, covering hosting, maintenance, unlimited content updates, a monthly security
  report and automated review generation. Drops to $145/month automatically
  once the build is paid off at month 24. If you paid the build
  upfront, the same plan is $145/month from day one.</li>
  <li><strong>Out-of-scope work.</strong> $85/hour, quoted and approved before it
  starts and invoiced after, for anything outside the original build.</li>
  <li><strong>Reviews.</strong> Being found first in the map pack comes from review
  count and recency as much as the build. Automated review generation is included in
  the Full Plan; see <a href="/services/review-automation/">how it works</a>.</li>
</ul>

<h2>A decision framework</h2>
<p>Spend the money on custom when at least two of these are true:</p>
<ol>
  <li>One customer is worth more than the build cost.</li>
  <li>You compete on search, and your competitors all use the same theme.</li>
  <li>The site has a job beyond existing: booking, intake, payments.</li>
  <li>You are losing mobile visitors to load time today.</li>
  <li>You want to own the asset rather than rent it indefinitely.</li>
</ol>
<p>If none of those are true, buy a $16/month builder subscription and
spend the difference on something that grows the business faster. That
advice costs us work and it is still the right advice.</p>

<h2>Why we publish the number</h2>
<p>Quoting "it depends" is a way of finding out what you will pay before
saying what it costs. You can see our prices without talking to anyone,
and if the range does not fit, you have lost nothing but a minute.</p>
""",
    },
    {
        'slug': 'why-your-business-isnt-showing-up-on-google',
        'title': "Why Your Business Isn't Showing Up on Google",
        'summary': (
            'Seven reasons, ordered by how often they are actually the '
            'cause, with the fix for each and how long it takes.'
        ),
        'related_url': '/services/review-automation/',
        'related_label': 'See automated review generation →',
        'body': """
<p>You searched for your own business, did not find it, and now you are
here. Below are the seven causes, roughly in the order they turn out to
be the real one, with how to check each yourself.</p>

<h2>1. You are searching in a way nobody else does</h2>
<p>Searching your exact business name while logged in, from your own
office, on the network you always use, tells you almost nothing. Google
personalises heavily. Search the way a <em>customer</em> would (the
service plus the city) in a private window.</p>
<p><strong>Fix:</strong> none needed. Re-test properly first, before
spending money on a problem you might not have.</p>

<h2>2. You have no Google Business Profile, or it is half-built</h2>
<p>For local searches, the map block above the normal results is where
most clicks go. If you are not in it, you are effectively invisible
regardless of how good your website is. Most profiles have a name, an
address, and nothing else.</p>
<p><strong>Fix:</strong> claim it, then actually fill it in: correct
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
<a href="/audit/">free website audit</a> checks the basics in about 30 seconds. <strong>Timeline:</strong> fixes land in days; Google notices
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
losing people before they see anything, and Google knows.</p>
<p><strong>Fix:</strong> usually images and bloat. When we rebuilt this
site, the biggest mobile wins came from exactly those two things: smaller
images and less code on every page.</p>

<h2>7. Your listings disagree with each other</h2>
<p>An old suite number on one directory, a former phone number on
another. Google uses consistency as a confidence signal, and
inconsistency quietly suppresses you.</p>
<p><strong>Fix:</strong> pick one exact format and make every listing
match it. Tedious, unglamorous, effective.</p>

<h2>The order to work through it</h2>
<ol>
  <li>Re-test properly in a private window.</li>
  <li>Fix your Google Business Profile: fastest return.</li>
  <li>Run a technical audit and fix what it finds.</li>
  <li>Fix your speed if it is bad.</li>
  <li>Split services onto real pages.</li>
  <li>Clean up listings.</li>
  <li>Wait. It takes months, and anyone who says otherwise is selling.</li>
</ol>

<p>One thing worth saying plainly: nobody can guarantee you a ranking.
Anyone who does is either targeting keywords nobody searches or is not
being straight with you.</p>
""",
    },
    {
        'slug': 'how-much-does-law-firm-web-design-cost',
        # Sept 2026 plan M-3.05: kept reachable, but noindexed, unlisted
        # and shown with an archived notice.
        'is_archived': True,
        'title': 'How Much Does Law Firm Web Design Cost?',
        'summary': (
            'What attorneys actually pay, why the vendor platforms cost '
            'more than the invoice says, and the contract questions to '
            'ask before signing anything.'
        ),
        'related_url': '/services/web-design/',
        'related_label': 'See what we build now →',
        'body': """
<p>Law firm websites are priced differently from other small-business
sites, and not always for good reasons. Here is what the options
actually cost.</p>

<h2>The three ways firms buy a website</h2>

<h3>Vendor platforms: $200–$1,500/month, indefinitely</h3>
<p>The legal-marketing platforms. You pay monthly, forever, and in most
arrangements you do not own the site. Stop paying and it disappears,
along with the rankings you spent years earning. Over five years at
even $300/month that is $18,000, and you finish with nothing you can
take with you.</p>

<h3>General freelancer: $800–$2,500 once</h3>
<p>Cheapest up front. The risk is that legal has requirements a general
designer will not know to ask about: practice-area structure, bar
advertising rules, and intake that handles sensitive facts properly.</p>

<h3>Custom build: typically $3,000–$6,000 once</h3>
<p>The most durable option, if you find the right developer. You own
the code, the content and the domain, and the site keeps working
whether or not you keep paying anyone afterward. Price depends heavily
on scope: how many practice-area pages, how much custom functionality,
how much of the copy you are supplying versus having written. Extra
practice-area pages typically add to the quote rather than coming
free.</p>

<h2>The number that actually matters</h2>
<p>Not the invoice. The five-year total, and what you hold at the end
of it.</p>
<ul>
  <li><strong>Vendor at $300/month:</strong> $18,000, and you own
  nothing.</li>
  <li><strong>Custom at roughly $4,000 once, plus hosting at around
  $150–$300/year:</strong> about $5,000 over five years, and you own
  everything.</li>
</ul>
<p>Even adding ongoing maintenance for all five years, the custom route
finishes with an asset instead of a cancelled subscription.</p>

<h2>What drives a law firm quote up</h2>
<ul>
  <li><strong>Practice areas.</strong> The main driver. Each area needs
  a real page to rank for its own searches. A combined list ranks for
  none of them.</li>
  <li><strong>Attorney bios.</strong> Each attorney is effectively a
  page. Prospects search for people by name.</li>
  <li><strong>Intake.</strong> A form carrying facts about a legal
  problem before any engagement letter exists deserves proper
  handling: validated server-side, rate-limited, delivered straight to
  the firm.</li>
  <li><strong>Migration.</strong> Leaving a vendor means mapping every
  old URL so the rankings survive the move.</li>
</ul>

<h2>Questions to ask before you sign anything</h2>
<ol>
  <li>Do I own the source code?</li>
  <li>Who holds the domain registration? (Check this today: it is where
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
state bar requires is the attorney's call. This is information, not
legal advice.</p>

<p><em>A note on who wrote this: Aspired Websites now builds for
<a href="/services/web-design/">HVAC contractors</a>, not law firms.
This article stays up because the math and the questions above are
still accurate no matter who ends up building the site.</em></p>
""",
    },
]


class Command(BaseCommand):
    help = 'Seed the first /insights/ articles.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Overwrite existing articles, discarding admin edits.')

    def handle(self, *args, **options):
        created = updated = skipped = 0

        for data in ARTICLES:
            slug = data['slug']
            existing = Article.objects.filter(slug=slug).first()

            if existing and not options['force']:
                if existing.status != 'published':
                    existing.status = 'published'
                    existing.save()
                    updated += 1
                    self.stdout.write(f'  published: {slug}')
                else:
                    skipped += 1
                continue

            fields = dict(data)
            fields.pop('slug')
            fields['body'] = fields['body'].strip()
            fields['status'] = 'published'

            if existing:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.save()
                updated += 1
                self.stdout.write(f'  overwritten: {slug}')
            else:
                Article.objects.create(
                    slug=slug, published_at=timezone.now(), **fields)
                created += 1
                self.stdout.write(f'  created: {slug}')

        self.stdout.write(self.style.SUCCESS(
            f'insights — created {created}, updated {updated}, '
            f'unchanged {skipped}'))
