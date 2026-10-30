"""
Rename Lead.website_mobile_score -> Lead.website_best_practices_score.

The field was never a mobile score. ``enricher._run_pagespeed`` has
always assigned it the PageSpeed **best-practices** category, under a
comment admitting it was "the closest to mobile in our 3-column slot".

That would be a cosmetic mislabel if it had stayed internal, but two
readers put the number into outbound cold email as a mobile score --
``icebreaker`` emitted "Their site scores X/100 on mobile" and
``sender`` fed the AI generator "- Their site PageSpeed (mobile):
X/100". So we were stating something false to prospects. Those two call
sites now use ``website_performance_score``, which is genuinely a
mobile measurement because every PageSpeed call this project makes
passes ``strategy=mobile``.

Written by hand rather than via makemigrations because the auto-detector
asks the rename question interactively. RenameField preserves the column
data; nothing is re-enriched.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('outreach', '0027_apifyrun_billable'),
    ]

    operations = [
        migrations.RenameField(
            model_name='lead',
            old_name='website_mobile_score',
            new_name='website_best_practices_score',
        ),
    ]
