"""
Sept 2026 buyer-persona review follow-up (owner-approved): evening-only
weekday call hours read as "side hustle" to office-manager buyers, and a
Tuesday-morning call was impossible to book. Adds Tuesday and Thursday
9:00 AM – 12:00 PM ET windows so office staff can book during business
hours, while the default 4–8 PM windows keep serving owner-operators.

Guarded: rows are added only if NO weekday morning window (start before
noon, Mon–Fri) exists yet, so an admin who already manages windows is
never second-guessed, and a re-run is a no-op.
"""
import datetime

from django.db import migrations

MORNINGS = [
    (1, datetime.time(9, 0), datetime.time(12, 0)),   # Tuesday
    (3, datetime.time(9, 0), datetime.time(12, 0)),   # Thursday
]


def forwards(apps, schema_editor):
    AvailabilityWindow = apps.get_model('scheduler', 'AvailabilityWindow')
    has_weekday_morning = AvailabilityWindow.objects.filter(
        day_of_week__lt=5, start_time__lt=datetime.time(12, 0), active=True,
    ).exists()
    if has_weekday_morning:
        return
    for day, start, end in MORNINGS:
        AvailabilityWindow.objects.create(
            day_of_week=day, start_time=start, end_time=end,
            timezone='America/New_York', active=True,
        )


def backwards(apps, schema_editor):
    AvailabilityWindow = apps.get_model('scheduler', 'AvailabilityWindow')
    for day, start, end in MORNINGS:
        AvailabilityWindow.objects.filter(
            day_of_week=day, start_time=start, end_time=end,
            timezone='America/New_York',
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
