"""
Sept 2026 audit follow-up: Google truncates meta descriptions around
155-160 characters, and these DB-held values ran 163-203, so their
endings were never read. Case-study and article summaries double as the
meta description (and the card text), so they are tightened at the
source rather than truncated.

Guarded like every content migration: a value is replaced only while it
still holds the exact wording below, so an admin edit is never
overwritten. seed_insights / seed_case_studies / case_study_deep_dive
carry the same new wording for fresh installs; City copy has no seed
command (0006/0007 set it directly), so this migration is its source.
"""
from django.db import migrations

CITY_META = {
    'san-antonio': (
        'Custom websites for San Antonio HVAC contractors, built for emergency calls, seasonal demand and the Google map pack. Hand-coded, security-first, you own the code.',
        'Custom websites for San Antonio HVAC contractors, built for emergency calls, summer demand and the map pack. Hand-coded, and you own the code.',
    ),
    'atlanta': (
        'Custom web design for Atlanta HVAC contractors from a Georgia company. Built for emergency-call urgency and local search. Hand-coded, security-hardened, you own every file.',
        'Web design for Atlanta HVAC contractors from a Georgia company. Built for emergency calls and local search. Hand-coded, and you own every file.',
    ),
    'warner-robins': (
        'Custom web design for HVAC contractors in Warner Robins, Georgia. We’re based here. Hand-coded, security-hardened sites built for emergency-call urgency, and you own every file.',
        'Web design for HVAC contractors in Warner Robins, Georgia, from a company based here. Hand-coded, built for emergency calls, and you own every file.',
    ),
}

ARTICLE_SUMMARY = {
    'hvac-seasonal-demand-website': (
        'Calls triple in a heat wave and vanish in October. What that means for how a contractor website is built, what to do with the slow months, and why next summer is won in the off-season.',
        'Calls triple in a heat wave and vanish in October. What that means for how a contractor website is built, and why next summer is won in the off-season.',
    ),
    'google-business-profile-multiple-locations': (
        'Two branches, one phone number, and a map pack that only shows one of you. How multi-location contractors should structure profiles, pages, and review flow, and the shortcut that gets listings suspended.',
        'Two branches, one phone number, and a map pack that shows only one of you. How to set up profiles, pages and review routing for every location.',
    ),
    'google-review-gating-rules': (
        'Asking only your happy customers for reviews feels smart, and it is exactly what gets Google profiles penalized. What the rule says, and how to grow reviews without risking your listing.',
        'Asking only happy customers for reviews feels smart, and it is exactly what gets Google profiles penalized. What the rule says and how to stay inside it.',
    ),
}

ARTICLE_TITLE = {
    'google-business-profile-multiple-locations': (
        'Google Business Profile for Multi-Location Home-Service Companies',
        'Google Business Profile for Multi-Location Contractors',
    ),
}

CASE_STUDY_SUMMARY = {
    'whitehead-wellness': (
        'A membership platform for a wellness coach who wanted progress to feel human: seven Guardians, crests you earn rather than buy, and a real person reading every submission.',
        'A membership platform for a wellness coach who wanted progress to feel human: seven Guardians, crests you earn, and a real person reading every submission.',
    ),
    'moonieful-designs': (
        'The website and client platform for a brand-clarity studio: a focused homepage, a shop for the founder’s book, and the portal the studio runs its client work through.',
        'The website and client platform for a brand-clarity studio: a focused homepage, a shop for the founder’s book, and the portal the studio runs on.',
    ),
}


def _apply(model, field, mapping):
    for slug, (old, new) in mapping.items():
        model.objects.filter(slug=slug, **{field: old}).update(**{field: new})


def forwards(apps, schema_editor):
    _apply(apps.get_model('public', 'City'), 'meta_description', CITY_META)
    _apply(apps.get_model('public', 'Article'), 'summary', ARTICLE_SUMMARY)
    _apply(apps.get_model('public', 'Article'), 'title', ARTICLE_TITLE)
    _apply(apps.get_model('clients', 'CaseStudy'), 'summary', CASE_STUDY_SUMMARY)


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0019_copy_voice_sweep'),
        ('clients', '0074_food_trucks_count_reconciled'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
