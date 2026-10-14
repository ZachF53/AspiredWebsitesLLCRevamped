"""Oct 2026: the review system is sold standalone, so the one article
that states it is Full-Plan-only has to stop saying that.

"How Much Does a Custom Website Cost?" is one of the better-performing
pages in Search Console, and its Reviews bullet told readers automated
review generation was "included in the Full Plan" — i.e. that the only
way to buy it was to buy a build. That is the exact belief this
repositioning exists to remove.

Article.body is raw stored HTML rendered with `|safe`; it never passes
through the template engine, so it cannot read a live ServiceTier price
the way a real template can. The price is therefore hardcoded here and
in seed_insights.py, and public/tests_brand_consistency.py asserts both
against the live rows on every test run so this can't silently go stale
(see ArticlePriceAlignmentTests there).

Follows 0016's pattern: a targeted, whitespace-insensitive replacement
that applies only while the old wording is still present. Running it
twice, or on a row already edited through the admin, changes nothing.
"""

import re

from django.db import migrations


def _flex(old):
    """Regex for `old` that tolerates any run of whitespace."""
    parts = [re.escape(p) for p in old.split()]
    return r'\s+'.join(parts)


OLD_BULLET = (
    'Automated review generation is included in the Full Plan; see '
    '<a href="/services/review-automation/">how it works</a>.'
)
NEW_BULLET = (
    'Automated review generation is $97/month on its own, with no website '
    'build required, and it is included at no extra cost in the Full Plan; '
    'see <a href="/services/review-automation/">how it works</a>.'
)


def forwards(apps, schema_editor):
    Article = apps.get_model('public', 'Article')
    pattern = _flex(OLD_BULLET)
    for article in Article.objects.filter(
            slug='how-much-does-a-custom-website-cost'):
        if not article.body or not re.search(pattern, article.body):
            continue
        article.body = re.sub(
            pattern, lambda _m: NEW_BULLET, article.body, count=1)
        article.save(update_fields=['body'])


def backwards(apps, schema_editor):
    Article = apps.get_model('public', 'Article')
    pattern = _flex(NEW_BULLET)
    for article in Article.objects.filter(
            slug='how-much-does-a-custom-website-cost'):
        if not article.body or not re.search(pattern, article.body):
            continue
        article.body = re.sub(
            pattern, lambda _m: OLD_BULLET, article.body, count=1)
        article.save(update_fields=['body'])


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0021_phone_number_change'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
