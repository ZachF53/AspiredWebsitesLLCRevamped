"""Seed the SiteContent singleton with the answers the owner gave on
2026-09-25 (IMPLEMENTATION_GUIDE.md §B): the Google reviews link is
confirmed and the 210 number receives texts. Everything else stays
blank, so every gated section keeps rendering nothing.

Only fills a field that is still at its default, so re-running (or
running after the owner has edited the row) never overwrites an edit.
"""

from django.db import migrations

GOOGLE_REVIEWS_URL = 'https://g.co/kgs/q5dqyQ1'


def seed(apps, schema_editor):
    SiteContent = apps.get_model('public', 'SiteContent')
    row = SiteContent.objects.order_by('pk').first() or SiteContent.objects.create()
    changed = []
    if not row.google_reviews_url:
        row.google_reviews_url = GOOGLE_REVIEWS_URL
        changed.append('google_reviews_url')
    if not row.phone_accepts_sms:
        row.phone_accepts_sms = True
        changed.append('phone_accepts_sms')
    if changed:
        row.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0013_sitecontent'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
