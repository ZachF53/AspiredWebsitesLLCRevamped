"""
Sept 2026 buyer-persona review follow-up: the Food Trucks case study said
"21 Food trucks with full profiles" while foodtrucksofsa.com's homepage
says "Browse 8+ local vendors" — a checkable mismatch on a page titled
"Measured, Not Claimed". Reworded so both statements are visibly true:
21 profiles built, 8+ featured live at a time.

Guarded like every content migration: each field is replaced only while
it still holds the exact old wording, so an admin edit is never
overwritten.
"""
from django.db import migrations

OLD_LABEL = 'Food trucks with full profiles'
NEW_LABEL = 'Truck profiles built (8+ featured live at a time)'

OLD_RESULTS = (
    'A directory the community can actually use on a phone: 21 '
    'trucks with full menus, a live map, reviews, and a way for '
    'local businesses to book a truck for an event, with every '
    'truck page structured for search.'
)
NEW_RESULTS = (
    'A directory the community can actually use on a phone: 21 truck '
    'profiles with full menus (the homepage features the trucks that '
    'are active right now), a live map, reviews, and a way for local '
    'businesses to book a truck for an event, with every truck page '
    'structured for search.'
)


def forwards(apps, schema_editor):
    CaseStudy = apps.get_model('clients', 'CaseStudy')
    study = CaseStudy.objects.filter(slug='food-trucks-of-san-antonio').first()
    if study is None:
        return
    changed = False
    if study.results == OLD_RESULTS:
        study.results = NEW_RESULTS
        changed = True
    scorecard = study.scorecard or {}
    for vital in scorecard.get('vitals', []):
        if isinstance(vital, list) and len(vital) == 2 and vital[1] == OLD_LABEL:
            vital[1] = NEW_LABEL
            study.scorecard = scorecard
            changed = True
    if changed:
        study.save(update_fields=['results', 'scorecard'])


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0073_case_study_deep_dive_content'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
