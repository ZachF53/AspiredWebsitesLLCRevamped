"""Point the live Google review sync at the Aspired Websites listing.

Found with a Places Text Search for "Aspired Websites" with
includePureServiceAreaBusinesses=True (it is a service-area business,
so it does not appear without that flag): "Aspired Websites LLC",
aspiredwebsites.com, (210) 201-0375. Guarded: only fills a blank field.
"""

from django.db import migrations

PLACE_ID = 'ChIJ55Zkmfj3XIYRfPuHV0AzDOw'


def forwards(apps, schema_editor):
    SiteContent = apps.get_model('public', 'SiteContent')
    SiteContent.objects.filter(google_place_id='').update(
        google_place_id=PLACE_ID)


class Migration(migrations.Migration):
    dependencies = [('public', '0025_google_reviews')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
