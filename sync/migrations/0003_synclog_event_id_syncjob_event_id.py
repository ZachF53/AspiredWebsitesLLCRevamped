import uuid

from django.db import migrations, models


def _populate_event_ids(apps, schema_editor):
    SyncJob = apps.get_model('sync', 'SyncJob')
    for job in SyncJob.objects.filter(event_id__isnull=True).iterator():
        job.event_id = uuid.uuid4()
        job.save(update_fields=['event_id'])


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
        # event_id needs a distinct value per EXISTING row before it can be
        # made unique. A plain AddField(default=uuid.uuid4, unique=True)
        # computes that default ONCE in Python and applies the same literal
        # value to every pre-existing row — harmless on SQLite (which
        # rewrites the table row-by-row through Python, calling the default
        # fresh each time) but a UniqueViolation on Postgres the moment
        # there's more than one existing SyncJob row.
        migrations.AddField(
            model_name='syncjob',
            name='event_id',
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(_populate_event_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='syncjob',
            name='event_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
