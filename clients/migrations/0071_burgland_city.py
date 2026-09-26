"""
Re-audit 2026-09-26 (plan M-5.08): Burgland Technologies was the one
case study with no city. Their site lists a San Antonio, TX address.

Guarded like the other content migrations: only fills the fields when
they are still empty, so an edit made in the admin is never overwritten.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    CaseStudy = apps.get_model('clients', 'CaseStudy')
    City = apps.get_model('public', 'City')
    study = CaseStudy.objects.filter(slug='burgland-technologies').first()
    if study is None:
        return
    changed = []
    if not study.location:
        study.location = 'San Antonio, TX'
        changed.append('location')
    if study.city_id is None:
        city = City.objects.filter(slug='san-antonio').first()
        if city is not None:
            study.city = city
            changed.append('city')
    if changed:
        study.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0070_casestudy_screenshot_mobile'),
        ('public', '0017_rename_full_plan_tiers'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
