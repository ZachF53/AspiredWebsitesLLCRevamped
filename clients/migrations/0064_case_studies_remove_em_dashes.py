# Sept 2026 voice pass: strip em dashes from the three CaseStudy rows
# that had them (burgland-technologies, moonieful-designs,
# food-trucks-of-san-antonio). Punctuation-only rewrite, no facts or
# claims changed. denis-law-group already had none, so it's untouched.
# Mirrors clients/management/commands/seed_case_studies.py so a future
# --force reseed matches.

from django.db import migrations


def strip_em_dashes(apps, schema_editor):
    CaseStudy = apps.get_model('clients', 'CaseStudy')

    CaseStudy.objects.filter(slug='burgland-technologies').update(
        challenge=(
            'Selling technology to people who understand technology '
            'sets a higher bar. A slow site, a broken layout or a '
            'missing security header is not a cosmetic problem to that '
            'audience. It is evidence.'
        ),
    )

    CaseStudy.objects.filter(slug='moonieful-designs').update(
        challenge=(
            'A design studio’s website is itself a portfolio piece. '
            'If the site looks templated, the work looks templated, '
            'no matter how good it is.\n\n'
            'The difficulty is restraint: the site has to be visually '
            'confident without competing with the projects it exists '
            'to display.'
        ),
        results=(
            'A portfolio that presents the studio’s work without '
            'talking over it, and an ongoing working relationship '
            'between the two studios.'
        ),
    )

    CaseStudy.objects.filter(slug='food-trucks-of-san-antonio').update(
        summary=(
            'A mobile-first directory for San Antonio’s food truck '
            'community: event listings, truck profiles and a location '
            'finder.'
        ),
        challenge=(
            'Food truck customers are almost always on a phone, often '
            'outdoors, often on patchy mobile data, and deciding where '
            'to eat in the next few minutes. A site that takes several '
            'seconds to load has already lost them.\n\n'
            'The content also changes constantly (trucks move, events '
            'come and go), so the site had to stay easy to update '
            'without a developer involved every time.'
        ),
    )


def noop_reverse(apps, schema_editor):
    """Data migration -- nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0063_casestudy_is_concept'),
    ]

    operations = [
        migrations.RunPython(strip_em_dashes, noop_reverse),
    ]
