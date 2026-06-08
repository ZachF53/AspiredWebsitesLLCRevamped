# Phase 1 onboarding refactor — credential taxonomy.
#
# Adds `credential_type` (sub-category slug used by the SetupTodo
# auto-completion in Phase 3) and `custom_label` (free-text "Other"
# descriptor). Updates the category choices to the new six-bucket
# taxonomy (social / cms / server / infra / google / other) and
# back-fills every existing row losslessly:
#
#     old category   →   new category    credential_type   custom_label
#     ─────────────────────────────────────────────────────────────────
#     server         →   server          'ssh' if is_ssh else 'other'
#     domain         →   infra           'domain_registrar'
#     google         →   google          'other'
#     social         →   social          'other'
#     email          →   infra           'email_workspace'
#     stripe         →   other           'other'         "Stripe / Payments"
#     custom         →   other           'other'         (existing label)
#
# Schema-alter the category column AFTER the data migration so the
# old values are still valid when the mapping runs.

from django.db import migrations, models


def forwards(apps, schema_editor):
    """Map old (category) → new (category, credential_type, custom_label)."""
    VaultCredential = apps.get_model('vault', 'VaultCredential')

    mapping = {
        'domain': ('infra', 'domain_registrar', ''),
        'email':  ('infra', 'email_workspace', ''),
        'stripe': ('other', 'other', 'Stripe / Payments'),
        'custom': ('other', 'other', ''),
        'google': ('google', 'other', ''),
        'social': ('social', 'other', ''),
        'server': ('server', None, ''),  # type set below
    }

    for cred in VaultCredential.objects.all().iterator():
        old = cred.category
        if old not in mapping:
            continue
        new_cat, new_type, new_custom = mapping[old]
        if old == 'server':
            new_type = 'ssh' if cred.is_ssh_credential else 'other'
        cred.category = new_cat
        cred.credential_type = new_type or 'other'
        if new_custom:
            cred.custom_label = new_custom
        cred.save(update_fields=['category', 'credential_type', 'custom_label'])


def backwards(apps, schema_editor):
    """Reverse: collapse new taxonomy back to old (best-effort)."""
    VaultCredential = apps.get_model('vault', 'VaultCredential')

    reverse = {
        'social': 'social',
        'cms':    'custom',
        'server': 'server',
        'infra':  'domain',  # most of this bucket was originally domain
        'google': 'google',
        'other':  'custom',
    }
    for cred in VaultCredential.objects.all().iterator():
        cred.category = reverse.get(cred.category, 'custom')
        cred.save(update_fields=['category'])


class Migration(migrations.Migration):

    dependencies = [
        ('vault', '0007_clientvault_account_new_opssession_account_new_and_more'),
    ]

    operations = [
        # Add the two new fields first (so the data-migration step can
        # write to them).
        migrations.AddField(
            model_name='vaultcredential',
            name='credential_type',
            field=models.CharField(blank=True, default='other', max_length=40),
        ),
        migrations.AddField(
            model_name='vaultcredential',
            name='custom_label',
            field=models.CharField(blank=True, max_length=100),
        ),
        # Backfill while the old choices are still valid on the column.
        migrations.RunPython(forwards, backwards),
        # Finally swap the column to the new choice set.
        migrations.AlterField(
            model_name='vaultcredential',
            name='category',
            field=models.CharField(
                choices=[
                    ('social', 'Social profile'),
                    ('cms', 'Website / CMS'),
                    ('server', 'Server / hosting'),
                    ('infra', 'Domain & infrastructure'),
                    ('google', 'Google services'),
                    ('other', 'Other'),
                ],
                default='other', max_length=20,
            ),
        ),
    ]
