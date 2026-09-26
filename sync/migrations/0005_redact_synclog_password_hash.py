"""Scrub client.account.password_hash out of every stored inbound bundle.

SyncLog.payload_received used to keep the whole Moonieful bundle verbatim,
including the client's password hash, readable by anyone with Django admin
access. sync.views._redact now strips it before saving; this cleans the
rows written before that."""

from django.db import migrations

REDACTED = '[redacted]'


def forwards(apps, schema_editor):
    SyncLog = apps.get_model('sync', 'SyncLog')
    for log in SyncLog.objects.exclude(payload_received={}).iterator():
        payload = log.payload_received
        if not isinstance(payload, dict):
            continue
        client = payload.get('client')
        account = client.get('account') if isinstance(client, dict) else None
        if isinstance(account, dict) and account.get('password_hash') not in (None, '', REDACTED):
            account['password_hash'] = REDACTED
            log.payload_received = payload
            log.save(update_fields=['payload_received'])


class Migration(migrations.Migration):

    dependencies = [
        ('sync', '0004_synclog_account_new'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
