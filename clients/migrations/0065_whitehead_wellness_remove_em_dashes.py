# Sept 2026 voice pass, part 2: the "whitehead-wellness" CaseStudy row
# exists only on prod (created directly via admin, never in
# seed_case_studies.py's STUDIES list), so migration 0064 never touched
# it. Caught by a live-content diff of prod after that deploy.
# Punctuation-only rewrite, no facts or claims changed.

from django.db import migrations


def strip_em_dashes(apps, schema_editor):
    CaseStudy = apps.get_model('clients', 'CaseStudy')

    CaseStudy.objects.filter(slug='whitehead-wellness').update(
        summary=(
            'A membership platform for a wellness coach who wanted '
            'progress to feel human: seven Guardians, crests you earn '
            'rather than buy, and a real person reading every submission.'
        ),
        challenge=(
            'Most wellness platforms are built for the version of a '
            'person who never has a hard week. They count things at you, '
            'shame a broken streak, and rank members against one '
            'another. Kate wanted the opposite: a membership her clients '
            'could have a bad week inside of without falling out of it, '
            'where a missed day is quietly absorbed rather than '
            'punished, and where progress is confirmed by a human being '
            'instead of an algorithm. The hard part was keeping real '
            'structure and real standards while stripping out every '
            'pressure mechanic that usually enforces them.'
        ),
        solution=(
            'A custom-built membership site organised around seven '
            'Guardians, one for each pillar of wellbeing: nutrition, '
            'movement, sleep, stress, community, lifestyle, and mental '
            'wellness. Members progress through five crest tiers on '
            'each Guardian, from Hatchling to Dragon Master, with time '
            'gates that cannot be rushed and every goal read and '
            'verified by Kate herself before the next one opens. A '
            'two-minute daily ritual is free forever, grace days cover '
            'missed days automatically with no action from the member, '
            'and the full goal library is readable before anyone is '
            'asked for a card. Four membership levels run from a free '
            'account up to direct one-to-one access, with member '
            'sign-in, the community wall, and the crest-verification '
            'workflow all handled in the build.'
        ),
        results=(
            'Launched as a complete membership product rather than a '
            'brochure site: free signup, four membership levels, '
            'community posting, the crest-verification flow, and the '
            'daily ritual were all live on day one. Every goal on every '
            'Guardian at every tier is readable before payment, a '
            'deliberate trust decision built into the structure of the '
            'site, not a page added to it.'
        ),
    )


def noop_reverse(apps, schema_editor):
    """Data migration -- nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0064_case_studies_remove_em_dashes'),
    ]

    operations = [
        migrations.RunPython(strip_em_dashes, noop_reverse),
    ]
