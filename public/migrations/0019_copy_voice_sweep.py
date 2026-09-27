"""
Sept 2026 voice sweep: removes em dashes and the phrasing that reads as
AI-written ("actually", "honestly", "genuinely", "straight answers",
"not just X", hype lines) from the copy stored in the database: City
pages, Articles, CaseStudies and SiteContent.

The same PAIRS were applied to the templates and seed sources in the
same commit, so fresh installs and existing databases end up identical.

Guarded like every content migration: each pair is a phrase-level
substitution that only fires where the exact old wording is still
present, so any sentence an admin has rewritten is left alone. Matching
tolerates re-wrapped whitespace and HTML entities (&rsquo;, &mdash;)
because the stored HTML is line-wrapped and entity-encoded in places.
"""
import json
import re

from django.db import migrations

# (old, new). En dashes in number ranges ($12–$20) are correct
# typography and deliberately untouched.
PAIRS = [
    # ── Homepage / shared ──
    ('No page builders. No bloat.', 'No page builders.'),
    ('That changes everything about how the site is built.',
     'It shows in how the site is built.'),
    ('Every site hardened against the threats most agencies don’t even know exist.',
     'Every site is hardened against the common attacks that take small-business sites down.'),
    ('Looks and works flawlessly on the phone in your client’s hand',
     'Built to work well on the phone in your customer’s hand'),
    ('how homeowners actually search for and book HVAC work',
     'how homeowners search for and book HVAC work'),
    ('Moonieful also refers clients to us — and still chose us',
     'Moonieful also refers clients to us, and still chose us'),
    ('(mail receiving only — we work from Warner Robins)',
     '(mail receiving only; we work from Warner Robins)'),

    # ── Web design ──
    ('jumps by about a third — and it only gets worse from there. Speed isn’t a nice-to-have. It’s the entire game.',
     'jumps by about a third, and it keeps climbing from there. On a phone, speed decides who gets the call.'),
    ('No drag-and-drop bloat.', 'No drag-and-drop builders.'),
    ('That’s pure waste.', ''),
    ('a $16/month builder is honestly better value',
     'a $16/month builder is the better value'),
    ('Every build ships with the same foundation: the parts that turn a site into a sales engine, not just a digital brochure.',
     'Every build ships with the same foundation.'),
    ('no bloated frameworks. Built to load fast on a phone on mobile data, not just on the developer’s wifi.',
     'no heavy frameworks. Built to load fast on a phone on mobile data, not only on office wifi.'),
    ('starts on day one — and the data travels with the site',
     'starts on day one, and the data travels with the site'),
    ('. One Site. Zero Surprises.', ', Start to Launch'),
    ('rendered in code that’s actually fast.', 'built in code that loads fast.'),
    ('so the launch is the version you actually want.',
     'so the launch is the version you want.'),
    ('One Build. Flat Pricing. No Games.', 'One Build, One Flat Price'),
    ('a builder is honestly cheaper and fine.', 'a builder is cheaper and does the job.'),

    # ── Review automation ──
    ('Reviews Are How HVAC Customers Actually Choose',
     'Reviews Are How HVAC Customers Choose'),
    ('signals a business that’s actually active right now',
     'signals a business that’s active right now'),
    ('requests are what actually get accounts penalized',
     'requests are what get accounts penalized'),
    ('>Straight Answers<', '>Compliance<'),
    ('when a job closes — ServiceTitan', 'when a job closes: ServiceTitan'),
    ('trigger the request — as simple as', 'trigger the request, as simple as'),
    ('whichever fits how you actually work', 'whichever fits how you work'),
    ('opt-outs are permanent — that customer', 'opt-outs are permanent: that customer'),
    ('Yes — it’s set up during the build', 'Yes. It’s set up during the build'),
    ('during onboarding — a webhook or Zapier link', 'during onboarding with a webhook or Zapier link'),
    ('while the job is fresh — which is exactly the step busy crews forget.',
     'while the job is fresh, which is the step busy crews forget.'),
    ('when a job closes — that covers', 'when a job closes. That covers'),

    # ── Hosting ──
    ('by the engineer who can actually fix your site — with a security report in your inbox every month proving it.',
     'by the engineer who can fix your site, with a security report in your inbox every month to show for it.'),
    ('What “Managed” Actually Means Here', 'What “Managed” Means Here'),
    ('runs on its own server — never shared hosting', 'runs on its own server: never shared hosting'),
    ('not a guarantee — and the monthly report shows you the actual number.',
     'not a guarantee, and the monthly report shows you the real number.'),
    ('an automated scan — dependency vulnerabilities', 'an automated scan covering dependency vulnerabilities'),
    ('Hosting + Security — {{', 'Hosting + Security: {{'),
    ('Full Plan — {{', 'Full Plan: {{'),
    ('until you approve — and if the first month', 'until you approve. If the first month'),
    ('Want Your Site Handled, Not Just Hosted?', 'Want Your Site Handled for You?'),
    ('Who actually does the work?', 'Who does the work?'),

    # ── Location pages (City rows) ──
    ('Here’s our actual setup.', 'Here’s how we work.'),
    ('How This Actually Works', 'How It Works'),
    ('Where We Actually Are', 'Where We Are'),
    ('when a project genuinely calls for it', 'when a project calls for it'),
    ('that’s what a website build actually is:', 'that’s what a website build is:'),
    ('What Actually Ships', 'What Every Build Includes'),
    ('we’ll tell you honestly if you’d be better served',
     'we’ll tell you if you’d be better served'),
    ('wherever the client is — here’s the recent work, live and inspectable.',
     'wherever the client is. Here’s the recent work, live for you to inspect.'),
    ('Actually Local', 'Based Here'),
    ('Based Here, Not Just Targeting Here', 'We Live and Work Here'),
    ('The difference matters more than the marketing usually admits.',
     'That matters more than most agencies let on.'),
    ('in person is genuinely on the table', 'in person is on the table'),
    ('What A Middle Georgia HVAC Company Actually Needs',
     'What A Middle Georgia HVAC Company Needs'),
    ('To Win The Map Pack, Not Just The Search Bar', 'To Win The Map Pack'),
    ('We’re actually based here.', 'We’re based here.'),
    ('We build for the areas you actually serve.', 'We build for the areas you serve.'),
    ('not just answer a panic call', 'as well as answer a panic call'),
    ('Straight Up', 'Where We Work'),

    # ── Pricing ──
    ('What does the build actually include?', 'What does the build include?'),
    ('before you sign — quoted, never guessed.', 'before you sign.'),
    ('The Fine Print, In Plain English', 'The Fine Print, Summarized'),
    ('>No Surprises<', '>Fine Print<'),

    # ── Portfolio / case studies ──
    ('Your business deserves a site that actually works. Let’s build it.',
     'Let’s build a site that books jobs for your business.'),
    ('business actually needs the site to do.', 'business needs the site to do.'),
    ('built to a standard a technical audience would actually inspect.',
     'built to hold up when a technical audience inspects it.'),
    ('A directory the community can actually use on a phone',
     'A directory the community can use on a phone'),
    ('titled for the search people actually make', 'titled for the searches people run'),
    ('A studio’s whole operating system, not just a portfolio', 'The system the studio runs on'),
    ('Every build starts with a straight conversation about what your',
     'Every build starts with a conversation about what your'),

    # ── Insights index / article chrome ──
    ('Insights: Straight Answers About Websites and Getting Found',
     'Insights: Websites and Getting Found on Google'),
    ('Straight answers on what a website actually costs, custom versus WordPress,',
     'What a website costs, custom versus WordPress,'),
    ('Straight answers on what websites cost,', 'What websites cost,'),
    ('Straight Answers About <span class="accent">Websites And Search</span>',
     'Notes on <span class="accent">Websites And Search</span>'),
    ('What things actually cost, what actually works, and what agencies usually leave out. Written by the engineer who builds the sites, not a content farm hitting a quota.',
     'What things cost, what works, and what agencies usually leave out. Written by the engineer who builds the sites.'),
    ('Book a Strategy Call and get a straight answer, including “you don’t need us for this” when that’s the truth.',
     'Book a Strategy Call. If you don’t need us for this, we’ll say so.'),

    # ── Articles: seasonal demand ──
    ('for it — and the website feels', 'for it, and the website feels'),
    ('one that doesn’t — Google’s own research', 'one that doesn’t. Google’s own research'),
    ('decided when the site is built — which is an off-season decision.',
     'decided when the site is built, and that is an off-season decision.'),
    ('meet them differently — maintenance', 'meet them differently: maintenance'),
    ('before the first heat wave — start it in June and you paid',
     'before the first heat wave. Start it in June and you’ve paid'),
    (' — and the reason the best month', ', which is why the best month'),

    # ── Articles: multi-location GBP ──
    ('review flow — and the shortcut', 'review flow, and the shortcut'),
    ('is invisible — no matter how many trucks', 'is invisible, no matter how many trucks'),
    ('Service-area businesses — most contractors — can hide',
     'Service-area businesses (most contractors) can hide'),
    ('its own page on your site — real content about', 'its own page on your site with real content about'),
    ('keyed off the job record — not', 'keyed off the job record, not'),
    ('Name, address, phone — consistent everywhere',
     'Name, address and phone should match everywhere'),
    ('Air – Macon', 'Air of Macon'),
    ('What it costs, honestly', 'What it costs'),
    ('is not a second website — it is a set of', 'is not a second website. It is a set of'),

    # ── Articles: review gating ──
    ('feels smart — and it is exactly', 'feels smart, and it is exactly'),
    ('What the rule actually says', 'What the rule says'),
    ('in exchange for a review — positive or not.', 'in exchange for a review, positive or not.'),
    ('What a penalty actually looks like', 'What a penalty looks like'),
    ('Here is the part contractors don’t expect: asked consistently, most customers who bother to respond',
     'Asked consistently, most customers who respond'),
    ('positive reviews anyway — people', 'positive reviews anyway. People'),
    ('offer anything — even a sticker — for a review?', 'offer anything for a review, even a sticker?'),
    ('on purpose — every customer,', 'on purpose: every customer gets'),
    ('triggered by the completed job — because', 'triggered by the completed job, because'),

    # ── Articles: not showing up on Google ──
    ('how often they are actually the cause', 'how often they turn out to be the cause'),
    ('claim it, then actually fill it in', 'claim it, then fill it in completely'),
    ('Tedious, unglamorous, effective.', 'It is tedious, and it works.'),
    ('One thing worth saying plainly: nobody can guarantee', 'Nobody can guarantee'),

    # ── Articles: custom website cost ──
    ('what actually moves the number, and where the cheaper options genuinely beat us.',
     'what moves the number, and where the cheaper options beat us.'),
    ('The four price tiers, honestly', 'The four price tiers'),
    ('Genuinely fine for a single page', 'Fine for a single page'),
    ('how homeowners actually search when a unit fails', 'how homeowners search when a unit fails'),

    # ── Articles: law firm (archived, still reachable) ──
    ('What attorneys actually pay', 'What attorneys pay'),
    ('what the options actually cost', 'what the options cost'),
    ('The number that actually matters', 'The number that matters'),

    # ── About ──
    ('a CISSP certification, the gold standard in the security industry.',
     'a CISSP certification, one of the most widely recognized credentials in security.'),
    ('someone who actually understands what it means to protect a business online.',
     'someone who knows what it takes to protect a business online.'),
    ('What this actually means for you:', 'What this means for you:'),
    ('the person who actually built it', 'the person who built it'),
    ('a $16/month builder is genuinely better value', 'a $16/month builder is the better value'),
    ('nobody can honestly promise those', 'nobody can promise those'),
    ('We’ll tell you honestly, including when the answer costs us',
     'We’ll tell you, including when the answer costs us'),
    ('Work With Someone Who Actually Cares.', 'Talk to the Person Who Builds It.'),
    ('from a one-engineer studio, stated plainly.', 'from a one-engineer studio.'),

    # ── Contact / booking ──
    ('No pressure. No jargon. Just a straight conversation about what your business needs.',
     'No pressure and no jargon, just a conversation about what your business needs.'),
    ('within 24 hours — no call required.', 'within 24 hours, no call required.'),
    ('Website Build — Pay in Full', 'Website Build: Pay in Full'),
    ('Website Build — 24-Month Installment', 'Website Build: 24-Month Installment'),
    (' — I already have a site', ': I already have a site'),
    ('multi-location project — let’s scope it', 'multi-location project: let’s scope it'),

    # ── Audit ──
    ('summarizes what they found in plain English.',
     'summarizes what they found in a few sentences.'),
]

_CHARS = {
    ' ': r'(?:\s|&nbsp;)+',
    '’': r'(?:’|\'|&rsquo;|&#8217;|&#39;|\\u2019)',
    '“': r'(?:“|"|&ldquo;|&quot;|\\u201c)',
    '”': r'(?:”|"|&rdquo;|&quot;|\\u201d)',
    '—': r'(?:—|&mdash;|&#8212;|\\u2014)',
    '–': r'(?:–|&ndash;|&#8211;|\\u2013)',
    '&': r'(?:&amp;|&)',
}


def pattern(old):
    return re.compile(''.join(_CHARS.get(ch, re.escape(ch)) for ch in old))


COMPILED = [(pattern(old), new) for old, new in PAIRS]


def sweep_text(value):
    for rx, new in COMPILED:
        value = rx.sub(lambda _m, n=new: n, value)
    return value


def _sweep_rows(model):
    text_fields = [f for f in model._meta.get_fields()
                   if getattr(f, 'get_internal_type', None)
                   and f.get_internal_type() in ('CharField', 'TextField')
                   and f.name not in ('slug',)]
    json_fields = [f for f in model._meta.get_fields()
                   if getattr(f, 'get_internal_type', None)
                   and f.get_internal_type() == 'JSONField']
    for row in model.objects.all():
        changed = []
        for f in text_fields:
            old = getattr(row, f.name) or ''
            new = sweep_text(old)
            if new != old:
                setattr(row, f.name, new)
                changed.append(f.name)
        for f in json_fields:
            old = getattr(row, f.name)
            if not old:
                continue
            raw = json.dumps(old, ensure_ascii=False)
            new = sweep_text(raw)
            if new != raw:
                setattr(row, f.name, json.loads(new))
                changed.append(f.name)
        if changed:
            row.save(update_fields=changed)


def forwards(apps, schema_editor):
    for app_label, model_name in (('public', 'City'), ('public', 'Article'),
                                  ('public', 'SiteContent'),
                                  ('clients', 'CaseStudy')):
        _sweep_rows(apps.get_model(app_label, model_name))


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0018_site_content_review_count_university'),
        ('clients', '0074_food_trucks_count_reconciled'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
