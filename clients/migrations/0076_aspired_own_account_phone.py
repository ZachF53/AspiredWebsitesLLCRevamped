# Oct 2026 phone change: 210-896-2536 -> 210-201-0375.
#
# Aspired Websites has its own Account row (the company as its own
# client, used for internal testing and for the Aspired-owned sites), and
# its phone field still carries the old number. Scoped by exact phone
# match rather than by account id so it is a no-op on any database where
# that row does not exist, and so it cannot touch a real client's number.

from django.db import migrations


NEW_BY_OLD = {
    '(210) 896-2536': '(210) 201-0375',
    '210-896-2536': '210-201-0375',
    '+12108962536': '+12102010375',
    '2108962536': '2102010375',
}


def _swap(apps, mapping):
    Account = apps.get_model('clients', 'Account')
    for old, new in mapping.items():
        Account.objects.filter(phone=old).update(phone=new)


def set_new_phone(apps, schema_editor):
    _swap(apps, NEW_BY_OLD)


def restore_old_phone(apps, schema_editor):
    _swap(apps, {v: k for k, v in NEW_BY_OLD.items()})


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0075_account_visible_plan_tiers'),
    ]

    operations = [
        migrations.RunPython(set_new_phone, restore_old_phone),
    ]
