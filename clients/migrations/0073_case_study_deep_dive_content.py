"""
Sept 2026: expanded case studies.

1. Fills the new scorecard / deep_dive fields from
   clients/case_study_deep_dive.py, only where they are still empty.
2. Corrects narrative copy that no longer matches the live sites,
   guarded like the other content migrations: a field is replaced only
   while it still holds the exact old wording, so an admin edit is never
   overwritten.
   - Burgland claimed HSTS and a content security policy; the live site
     sends neither (checked 2026-09-26).
   - Food Trucks claimed a deliberately low page weight; it measures
     about 1.4 MB on a phone.
   - Moonieful was described as a portfolio showcase; the live site is
     a brand-clarity studio with a book shop and client portal.
"""
from django.db import migrations

from clients.case_study_deep_dive import CONTENT

NARRATIVE_FIXES = {
    'burgland-technologies': {
        'solution': (
            'A hand-coded, security-hardened build: HTTPS with strict '
            'transport security, hardened headers, a real content security '
            'policy, and clean semantic markup that stands up to someone '
            'opening developer tools.',
            'A hand-coded Django build with no page builder and no CSS '
            'framework: six service pages and an Insights blog, every page '
            'carrying its own structured data, served over HTTPS on TLS 1.3 '
            'with headers that block framing and content-type sniffing, and '
            'clean semantic markup that stands up to someone opening '
            'developer tools.',
        ),
    },
    'food-trucks-of-san-antonio': {
        'solution': (
            'A lean, mobile-first build with the weight kept deliberately '
            'low so it holds up on a phone away from wifi.\n\n'
            'Truck profiles, event listings and a location finder are '
            'structured as real content rather than a static page, so the '
            'directory can grow without the layout fighting it.',
            'A Django and PostgreSQL platform rather than a static page: '
            'truck owners manage their own profiles and menus, a live map '
            'shows who is out right now, and every truck, cuisine and '
            'article has its own indexable page.\n\n'
            'Critical styles are written into the page, photos are resized '
            'on upload and served as WebP, and images further down wait '
            'until they are needed, so the first screen appears without '
            'the layout jumping around.',
        ),
        'results': (
            'A directory the community can actually use on a phone, built '
            'to stay fast as more trucks and events are added.',
            'A directory the community can actually use on a phone: 21 '
            'trucks with full menus, a live map, reviews, and a way for '
            'local businesses to book a truck for an event, with every '
            'truck page structured for search.',
        ),
    },
    'moonieful-designs': {
        'challenge': (
            'A design studio’s website is itself a portfolio piece. If the site looks templated, the work looks templated, no matter how good it is.\n\nThe difficulty is restraint: the site has to be visually confident without competing with the projects it exists to display.',
            'A brand-clarity studio’s website is itself proof of the service. If the site looks templated or muddled, the promise of clarity falls flat before anyone reads a word.\n\nThe studio also needed more than a brochure: somewhere to run client projects, sell the founder’s book, and hand finished clients on to a website build without re-entering everything.',
        ),
        'summary': (
            'A portfolio site for a creative studio where the work has to '
            'be the loudest thing on the page.',
            'The website and client platform for a brand-clarity studio: '
            'a focused homepage, a shop for the founder’s book, and the '
            'portal the studio runs its client work through.',
        ),
        'solution': (
            'A clean, deliberately quiet build that puts the project '
            'showcase first and keeps browsing frictionless, with a direct '
            'path for a prospective client to get in touch rather than a '
            'buried contact page.',
            'A deliberately quiet homepage that keeps attention on the '
            'studio’s message, with a direct path to a fit call rather '
            'than a buried contact page.\n\n'
            'Behind it, a custom Django platform: a client portal for '
            'projects, files and approvals, intake forms the founder builds '
            'herself, a Stripe-powered shop for her book, and a signed sync '
            'bridge that hands clients to Aspired Websites for their '
            'website build.',
        ),
        'results': (
            'A portfolio that presents the studio’s work without talking '
            'over it, and an ongoing working relationship between the two '
            'studios.',
            'A site that presents the studio without talking over it, a '
            'platform the studio runs on day to day, and an ongoing working '
            'relationship between the two studios.',
        ),
    },
}


def forwards(apps, schema_editor):
    CaseStudy = apps.get_model('clients', 'CaseStudy')
    for slug, content in CONTENT.items():
        study = CaseStudy.objects.filter(slug=slug).first()
        if study is None:
            continue
        changed = []
        if not study.scorecard and content.get('scorecard'):
            study.scorecard = content['scorecard']
            changed.append('scorecard')
        if not study.deep_dive and content.get('deep_dive'):
            study.deep_dive = content['deep_dive']
            changed.append('deep_dive')
        for field, (old, new) in NARRATIVE_FIXES.get(slug, {}).items():
            current = (getattr(study, field) or '').replace('\r\n', '\n')
            if current.strip() == old.strip():
                setattr(study, field, new)
                changed.append(field)
        if changed:
            study.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0072_casestudy_scorecard_deep_dive'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
