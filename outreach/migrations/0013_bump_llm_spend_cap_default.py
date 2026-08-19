"""Move an existing OutreachSettings row from the old $5 LLM cap to $10.

Changing a field's ``default`` only affects rows created afterwards, so
the singleton created under migration 0010 would keep sitting at $5.00
while the code and admin help text said $10.00.

Only touches rows still holding the exact previous default. A cap that
was deliberately set to anything else — including a deliberate 5.00
entered by hand after this ships — is left alone rather than silently
raised. Raising someone's spend ceiling without being asked is the one
mistake this migration must not make; the cost of being conservative is
that a hand-set $5.00 needs bumping manually.
"""

from decimal import Decimal

from django.db import migrations


OLD_DEFAULT = Decimal('5.00')
NEW_DEFAULT = Decimal('10.00')


def bump_cap(apps, schema_editor):
    OutreachSettings = apps.get_model('outreach', 'OutreachSettings')
    OutreachSettings.objects.filter(
        daily_ai_spend_cap_usd=OLD_DEFAULT,
    ).update(daily_ai_spend_cap_usd=NEW_DEFAULT)


def unbump_cap(apps, schema_editor):
    OutreachSettings = apps.get_model('outreach', 'OutreachSettings')
    OutreachSettings.objects.filter(
        daily_ai_spend_cap_usd=NEW_DEFAULT,
    ).update(daily_ai_spend_cap_usd=OLD_DEFAULT)


class Migration(migrations.Migration):

    dependencies = [
        ('outreach', '0012_outreachsettings_apify_max_results_per_run_and_more'),
    ]

    operations = [
        migrations.RunPython(bump_cap, unbump_cap),
    ]
