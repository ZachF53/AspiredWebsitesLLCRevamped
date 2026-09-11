import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sync', '0002_syncjob_account_new_syncjob_website_new'),
    ]

    operations = [
        migrations.AddField(
            model_name='synclog',
            name='event_id',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name='syncjob',
            name='event_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
