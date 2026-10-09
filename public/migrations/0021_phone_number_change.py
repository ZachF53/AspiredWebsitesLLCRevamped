# Oct 2026 phone change: 210-896-2536 -> 210-201-0375.
#
# The number is hardcoded in editable copy that lives in the database
# (City.cta_body_html on all three /locations/ pages, and potentially any
# Article body or SiteContent block an admin has edited since), so the
# source-file sweep alone leaves the old number rendering in production.
#
# This sweeps every CharField/TextField on the three copy-bearing models
# rather than re-pasting known-good HTML the way 0012 did. Prod copy has
# drifted from the seed migrations through the admin dashboard, so a
# targeted replace preserves those edits; a full overwrite would discard
# them.
#
# The 210 area code is unchanged, so PHONE_NOTE ("A San Antonio number
# that rings in Warner Robins, GA") and the /locations/ prose explaining
# the area code both remain true and are untouched.

from django.db import migrations


OLD_NEW = (
    ('210-896-2536', '210-201-0375'),
    ('210) 896-2536', '210) 201-0375'),
    ('2108962536', '2102010375'),
)

MODELS = ('City', 'Article', 'SiteContent')


def _text_field_names(model):
    return [
        f.name for f in model._meta.get_fields()
        if getattr(f, 'get_internal_type', None)
        and f.get_internal_type() in ('CharField', 'TextField')
    ]


def _sweep(apps, replacements):
    for model_name in MODELS:
        model = apps.get_model('public', model_name)
        fields = _text_field_names(model)
        if not fields:
            continue
        for obj in model.objects.all():
            changed = []
            for name in fields:
                value = getattr(obj, name, None)
                if not isinstance(value, str) or not value:
                    continue
                new_value = value
                for old, new in replacements:
                    new_value = new_value.replace(old, new)
                if new_value != value:
                    setattr(obj, name, new_value)
                    changed.append(name)
            if changed:
                obj.save(update_fields=changed)


def set_new_phone(apps, schema_editor):
    _sweep(apps, OLD_NEW)


def restore_old_phone(apps, schema_editor):
    _sweep(apps, tuple((new, old) for old, new in OLD_NEW))


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0020_meta_description_lengths'),
    ]

    operations = [
        migrations.RunPython(set_new_phone, restore_old_phone),
    ]
