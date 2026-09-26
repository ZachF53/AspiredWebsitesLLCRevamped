"""Sept 2026 implementation plan: align database-held public content.

Case studies, articles, city pages and pricing-card bullets live in the
database, and some of them have been edited on production through the
admin. So this migration never overwrites a whole field: every change is
a targeted replacement that applies only when the old wording is still
present (whitespace-insensitive, because HTML bodies wrap lines
differently). Running it twice, or on a row the owner has already
fixed, changes nothing. Tasks: M-1.02, M-1.09, M-2.11, M-3.05, M-4.07,
M-5.10 (see IMPLEMENTATION_GUIDE.md).
"""

import re

from django.db import migrations


def _flex(old):
    """Regex for `old` that tolerates any run of whitespace."""
    parts = [re.escape(p) for p in old.split()]
    return r'\s+'.join(parts)


def _replace(text, old, new):
    if not text:
        return text, False
    pattern = _flex(old)
    if not re.search(pattern, text):
        return text, False
    return re.sub(pattern, lambda _m: new, text, count=1), True


def _apply(obj, field, pairs):
    value = getattr(obj, field)
    changed = False
    for old, new in pairs:
        value, did = _replace(value, old, new)
        changed = changed or did
    if changed:
        setattr(obj, field, value)
    return changed


DENIS_SOLUTION = (
    'Aspired Websites maintains the firm’s existing WordPress site: '
    'security and plugin updates, uptime monitoring, backups, and content '
    'changes as the practice needs them, without interrupting the enquiries '
    'the site already brings in.'
)
DENIS_RESULTS = (
    'A maintenance and improvement engagement on a site Aspired did not '
    'build. There is no before-and-after rebuild to show here, and we '
    'don’t publish performance numbers for it. If you have a working '
    'WordPress site that just needs looking after, this is what that looks '
    'like.'
)

SECURITY_REPORT_TEXT = (
    'Monthly security report: an automated scan of your site, summarized '
    'in a one-page PDF emailed to you on the 1st.'
)


def forwards(apps, schema_editor):
    CaseStudy = apps.get_model('clients', 'CaseStudy')
    Article = apps.get_model('public', 'Article')
    City = apps.get_model('public', 'City')
    ServiceTier = apps.get_model('billing', 'ServiceTier')
    TierFeature = apps.get_model('billing', 'TierFeature')

    # M-1.02: the Denis Law Group study published an internal working
    # note ("see ... docs/brand_fact_matrix.md"). Replace the two fields
    # only while that internal wording is still there.
    for study in CaseStudy.objects.filter(slug='denis-law-group'):
        fields = []
        if re.search(r'brand_fact_matrix|itemised|not\s+itemized', study.solution or ''):
            study.solution = DENIS_SOLUTION
            fields.append('solution')
        if re.search(r'measurement\s+window|brand_fact_matrix', study.results or ''):
            study.results = DENIS_RESULTS
            fields.append('results')
        if fields:
            study.save(update_fields=fields)

    # M-2.11, M-4.07: the cost article and the Google-visibility article.
    for article in Article.objects.filter(slug='how-much-does-a-custom-website-cost'):
        if _apply(article, 'body', [
            ('<h2>What actually changes the price</h2> <p>Four things, in order of impact:</p>',
             '<h2>What changes the <em>work</em>, and when we’ll tell you the flat price doesn’t fit</h2>\n'
             '<p>The build is one flat price. These four things change how much work it is. '
             'If yours needs far more than a normal build, we’ll say so on the call and '
             'quote it rather than guess:</p>'),
            ('For law firms this is usually practice areas: each one needs its own real page.',
             'For an HVAC or home-service company this is usually service pages and '
             'service-area pages: each one needs its own real page.'),
            ('<strong>Out-of-scope work.</strong> $85/hour, invoiced before it starts, for anything outside the original build.',
             '<strong>Out-of-scope work.</strong> $85/hour, quoted and approved before it '
             'starts and invoiced after, for anything outside the original build.'),
            ('covering hosting, maintenance, unlimited content updates and automated review generation.',
             'covering hosting, maintenance, unlimited content updates, a monthly security '
             'report and automated review generation.'),
            ('<li><strong>SEO.</strong> A separate ongoing discipline. A well-built site can be found. '
             'Being found <em>first</em>, especially in the map pack, comes from reviews as much as it does '
             'from the build. See <a href="/services/review-automation/">automated review generation</a>.</li>',
             '<li><strong>Reviews.</strong> Being found first in the map pack comes from review '
             'count and recency as much as the build. Automated review generation is included in '
             'the Full Plan; see <a href="/services/review-automation/">how it works</a>.</li>'),
        ]):
            article.save(update_fields=['body'])

    for article in Article.objects.filter(slug='why-your-business-isnt-showing-up-on-google'):
        if _apply(article, 'body', [
            ('checks the basics in about a minute.', 'checks the basics in about 30 seconds.'),
            # The "4.1 s to 1.5 s" figure could not be reproduced by the
            # Sept 2026 Lighthouse baseline, so it is no longer stated as
            # a measured number (M-6.09).
            ('When we rebuilt this site, mobile load time went from 4.1 seconds to 1.5, measured before and after, not estimated.',
             'When we rebuilt this site, the biggest mobile wins came from exactly those two '
             'things: smaller images and less code on every page.'),
        ]):
            article.save(update_fields=['body'])

    # M-3.05: the law-firm cost post stays reachable but is archived.
    Article.objects.filter(
        slug='how-much-does-law-firm-web-design-cost', is_archived=False,
    ).update(is_archived=True)

    # M-1.05 / M-5.10: ownership claims carry "once it's paid off".
    for city in City.objects.filter(slug='warner-robins'):
        if _apply(city, 'secondary_html', [
            ('and you own it, so any developer can pick it up.',
             'and once it’s paid off you own it, so any developer can pick it up.'),
        ]):
            city.save(update_fields=['secondary_html'])

    # M-1.09: the monthly security report is included in every plan
    # (owner, 2026-09-25), so each card states it plainly, and the
    # Hosting + Security card now lists it too.
    for feature in TierFeature.objects.filter(
            tier__slug__in=['hvac-full-plan', 'hvac-plan-paid-in-full'],
            text__icontains='Included in every plan'):
        feature.text = SECURITY_REPORT_TEXT
        feature.save(update_fields=['text'])

    hosting = ServiceTier.objects.filter(slug='hvac-hosting-security').first()
    if hosting and not TierFeature.objects.filter(
            tier=hosting, text__icontains='security report').exists():
        last = TierFeature.objects.filter(tier=hosting).order_by('-sort_order').first()
        TierFeature.objects.create(
            tier=hosting, text=SECURITY_REPORT_TEXT,
            sort_order=(last.sort_order + 1) if last else 1)


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0015_article_is_archived'),
        ('clients', '0068_moonieful_sites_intake_complete'),
        ('billing', '0010_striperevenuesync'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
