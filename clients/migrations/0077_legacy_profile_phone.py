# Oct 2026 phone change, part two: the legacy ClientProfile mirror.
#
# 0076 updated the Account row, but production also still carries the
# pre-Account ClientProfile for Aspired itself ("Aspired Websites LLC" /
# Zachery Long) with the old number in its phone field. ClientProfile is
# on its way out (see LEGACY_OWNER_FK_HANDOFF.md) but it is live until
# that lands, so it gets corrected rather than left stale.
#
# Scoped by exact phone match, same as 0076, so it is a no-op where the
# row does not exist and cannot touch a real client's number.
#
# Deliberately NOT swept: reporting.ConversionEvent.element_text. One
# prod row is a real phone_click from 2026-10-05 whose element_text is
# the link label the visitor actually saw and clicked. Rewriting it would
# falsify the analytics record of what was on the page that day.

from django.db import migrations


NEW_BY_OLD = {
    '(210) 896-2536': '(210) 201-0375',
    '210-896-2536': '210-201-0375',
    '+12108962536': '+12102010375',
    '2108962536': '2102010375',
}


def _swap(apps, mapping):
    ClientProfile = apps.get_model('clients', 'ClientProfile')
    for old, new in mapping.items():
        ClientProfile.objects.filter(phone=old).update(phone=new)


def set_new_phone(apps, schema_editor):
    _swap(apps, NEW_BY_OLD)


def restore_old_phone(apps, schema_editor):
    _swap(apps, {v: k for k, v in NEW_BY_OLD.items()})


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0076_aspired_own_account_phone'),
    ]

    operations = [
        migrations.RunPython(set_new_phone, restore_old_phone),
    ]
