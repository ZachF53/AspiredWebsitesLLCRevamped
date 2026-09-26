"""
/pricing/ copy that has to carry live prices.

CLAUDE.md forbids hardcoding a price in a view or template, and the
pricing page's FAQ, fine print and total-cost table all quote numbers.
They are built here from the ServiceTier rows the page already loads, so
a price edit in the admin dashboard updates every sentence that mentions
it. The FAQ list is also the single source for the FAQPage JSON-LD, so
the structured data can never describe answers that aren't on the page.
"""

import json
from decimal import Decimal

from django.utils.safestring import mark_safe

from core.site_facts import (
    BUILD_TIMELINE, GUARANTEE_DAYS, GUARANTEE_REFUND_PERCENT,
    GUARANTEE_RETAINED_PERCENT,
)

INSTALLMENT_MONTHS = 24


def money(value):
    """$2,000 / $105 / $7,220 (no cents for whole-dollar amounts)."""
    if value is None:
        return ''
    value = Decimal(value)
    if value == value.to_integral_value():
        return f'${value:,.0f}'
    return f'${value:,.2f}'


def _price(tier):
    return Decimal(tier.price) if tier is not None else None


def cost_table(build_full, build_installment, full_plan, plan_paid_in_full,
               hosting_security):
    """Rows for the "What it costs over time" table, or [] if a tier is
    missing (a half-built table would be worse than none)."""
    full = _price(build_full)
    inst = _price(build_installment)
    fp = _price(full_plan)
    ppf = _price(plan_paid_in_full)
    host = _price(hosting_security)
    if None in (full, inst, fp, ppf, host):
        return {'rows': [], 'savings': None}

    months = INSTALLMENT_MONTHS
    rows = [
        {
            'path': 'Build paid in full + Full Plan',
            'at_signing': full + ppf,
            'at_signing_note': f'{money(full)} build + first {money(ppf)} month',
            'monthly': f'{money(ppf)}/mo',
            'total_24': full + ppf * months,
            'total_36': full + ppf * (months + 12),
            'includes': 'Site, hosting, unlimited content updates, security '
                        'patching, monthly security report, automated review generation',
        },
        {
            'path': 'Installment + Full Plan',
            'at_signing': fp,
            'at_signing_note': f'first {money(fp)} month',
            'monthly': f'{money(fp)}/mo, then {money(ppf)}/mo after month {months}',
            'total_24': fp * months,
            'total_36': fp * months + ppf * 12,
            'includes': 'Same as above. We own the site until month '
                        f'{months}, then it is yours',
        },
        {
            'path': 'Build paid in full + Hosting + Security only',
            'at_signing': full + host,
            'at_signing_note': f'{money(full)} build + first {money(host)} month',
            'monthly': f'{money(host)}/mo',
            'total_24': full + host * months,
            'total_36': full + host * (months + 12),
            'includes': 'Site, hosting, security patching, monthly security '
                        'report. Content edits billed hourly; no review system',
        },
    ]
    for row in rows:
        for key in ('at_signing', 'total_24', 'total_36'):
            row[f'{key}_display'] = money(row[key])
    savings = rows[1]['total_24'] - rows[0]['total_24']
    return {'rows': rows, 'savings': money(savings) if savings > 0 else None}


def pricing_faqs(build_full, build_installment, full_plan, plan_paid_in_full,
                 hosting_security, hourly_display, site_content=None):
    """[(question, answer)] in page order. Plain text: the same strings go
    into the FAQPage schema."""
    full = money(_price(build_full)) or 'the build price'
    inst = money(_price(build_installment)) or 'the monthly installment'
    ppf = money(_price(plan_paid_in_full)) or 'the paid-in-full plan rate'
    host = money(_price(hosting_security)) or 'the hosting rate'
    rate = hourly_display or 'our hourly rate'
    m = INSTALLMENT_MONTHS

    faqs = [
        ('What happens at month 24?',
         f'On the Full Plan, the {inst} build portion ends and the rate drops '
         f'automatically to {ppf}/month for hosting, maintenance, unlimited '
         'content updates, security patching, the monthly security report, and '
         'automated review generation. No action needed on your end. It’s '
         'month-to-month after that, and you can cancel anytime.'),
        ('What does ownership mean before payoff?',
         'Aspired Websites LLC retains ownership of the website until it’s '
         f'paid in full, either the {full} upfront or the {m} monthly payments. '
         'The site stays live and working for your business the whole time, and '
         'ownership transfers to you once the build is paid off. If installments '
         'stop, the site stays up while we sort it out; we’ll always talk '
         'before anything is suspended (14 days’ notice per our Terms).'),
        ('What counts as an unlimited content update?',
         'Text changes, photo swaps, hours, service areas, pricing updates, new '
         'promotions: routine edits to the site you already have. It does not '
         'include a redesign, new pages built from scratch, or new functionality, '
         f'which are quoted separately at {rate}, approved before any work starts.'),
        ('Is there a contract?',
         f'The build is a {m}-month term if you choose the installment. The Full '
         'Plan and Hosting + Security are month-to-month with 30 days’ notice. '
         'No annual contracts.'),
        ('When am I charged?',
         'Everything starts the day you sign: the build payment (in full, or the '
         f'first of {m} installments) and your first month of the Full Plan or '
         'Hosting + Security. After that, monthly charges run on the same date '
         'each month.'),
        ('How does the 30-day guarantee work?',
         f'For {GUARANTEE_DAYS} days from the day you sign, you can cancel for any '
         f'reason. We refund {GUARANTEE_REFUND_PERCENT}% of everything you’ve '
         f'paid, keep {GUARANTEE_RETAINED_PERCENT}% for the work already done, and '
         'cancel any remaining payments. After 30 days our fix-it commitment '
         'applies: anything we built that doesn’t work as agreed gets fixed '
         'at no charge.'),
        ('Do I own my website?',
         'Once the build is paid in full, yes, completely. Source code, content, '
         'domain and data are yours to take anywhere. Your domain is registered in '
         'your name from day one; we never hold it.'),
        ('What is the monthly security report?',
         'On the first of each month we email you a one-page summary of an '
         'automated scan of your site: dependency vulnerabilities, file integrity, '
         'SSL certificate status, security headers, and uptime. If something needs '
         'attention, we fix it; that’s covered by your plan. It’s '
         f'included in the Full Plan and in Hosting + Security ({host}/mo).'),
        ('Can I move the site later?',
         'Yes, once it’s paid off. Standard code on standard hosting, so any '
         f'competent developer can take it over. Hands-on migration help is {rate}; '
         'the files themselves are free.'),
        ('How long does a build take?',
         f'{BUILD_TIMELINE[0].upper()}{BUILD_TIMELINE[1:]}, depending on how '
         'quickly content comes back: discovery, structure, design and build, '
         'review, launch. You always know which stage you’re in.'),
        ('Can I keep my current domain and email working during the switch?',
         'Yes. At launch we point your domain at the new site; your email '
         'isn’t touched.'),
        ('Can I approve drafts and send photos from my phone?',
         'Yes. Staging links, approvals and photo uploads all work from a phone.'),
        ('Can I add review automation later?',
         f'Yes. Hosting + Security clients can move to the Full Plan any time; '
         'it’s month-to-month.'),
        ('Running several locations or a larger site?',
         'That’s scoped on the call: service-area pages, moving your old page '
         'addresses across, and which Google Business Profile each review request '
         'goes to. There’s no price list for it, and we’d rather quote it '
         'than guess.'),
        ('Is SEO included?',
         'Yes. Clean markup, schema, sitemap, headings and fast load are part of '
         'every build, not a separate product. We never promise rankings.'),
        ('What if I stop paying the Full Plan?',
         'Once the build is paid off, the site keeps working. The Full Plan buys '
         'updates, monitoring, backups, review generation, and support, not the '
         'right to keep your own website online.'),
        ('What counts as out of scope?',
         'Anything outside the agreed build or the Full Plan’s content '
         f'updates: {rate}, quoted and approved before anything starts, invoiced '
         'after it’s done. No surprise invoices.'),
    ]
    if site_content is not None:
        faqs.extend(site_content.policy_faqs)
    return faqs


def faq_schema(faqs):
    """FAQPage JSON-LD for [(q, a)], safe to drop into a script tag."""
    data = {
        '@context': 'https://schema.org',
        '@type': 'FAQPage',
        'mainEntity': [
            {'@type': 'Question', 'name': q,
             'acceptedAnswer': {'@type': 'Answer', 'text': a}}
            for q, a in faqs
        ],
    }
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    # A literal "</" inside a script element ends it early.
    return mark_safe(raw.replace('</', '<\\/'))
