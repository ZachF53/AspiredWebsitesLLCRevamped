"""
Seed the four public case studies (Master Plan §11).

The portfolio page used to hardcode these as markup, which meant all
four projects shared one URL. They are data now so each can have its
own indexable page. This command carries the existing copy across and
is idempotent — safe to re-run on every deploy.

    python manage.py seed_case_studies
    python manage.py seed_case_studies --force   # overwrite edits

IMPORTANT — no invented numbers. Master Plan §15 forbids fabricated
results, statistics and testimonials. None of these four have
measured before/after data:

  * Denis Law Group — SUPERSEDED 2026-08-16. This entry previously
    described a new practice launch that Aspired built from scratch,
    citing owner confirmation of 2026-08-02. Later owner direction
    (BRAND_REMEDIATION_HANDOFF.md, recorded APPROVED in
    docs/brand_fact_matrix.md) establishes that Aspired did NOT build
    the site: it is an existing WordPress site that Aspired maintains
    and has improved. The build narrative was removed everywhere.

    Do not reintroduce "built", "built from scratch", "hand-coded",
    "no template", "no page builder", or "new practice launch" for this
    study. The reported ~2-3 contacts per week is NOT published here —
    its source, measurement window, baseline, attribution and client
    approval are still unproven.

Seeding a row is not a production fix. `--force` overwrites edits, and
existing production rows keep their old copy until they are corrected
explicitly — use `remediate_case_studies` (dry-run by default) for that.

So every `metrics` slot is left empty and every `testimonial_quote` is
blank until a real, attributable quote exists. The template renders
those blocks only when populated, so an honest study simply shows
fewer sections rather than placeholder numbers.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from clients.models import CaseStudy

STUDIES = [
    {
        'slug': 'denis-law-group',
        'title': 'Denis Law Group',
        'business_type': 'Law Firm',
        'location': 'San Antonio, TX',
        'live_url': 'https://denislawgroup.com/',
        'card_gradient': 'gradient-blue',
        'engagement_type': 'maintained',
        'platform': 'WordPress',
        'summary': (
            'An existing WordPress website for a law practice, '
            'maintained and improved by Aspired Websites.'
        ),
        'challenge': (
            'The firm already had a WordPress website and needed it '
            'looked after rather than replaced. Ongoing maintenance on a '
            'live legal website has to keep the site available and '
            'current without interrupting the enquiries it already '
            'receives.'
        ),
        'solution': (
            'Aspired Websites took over ongoing maintenance of the '
            'existing WordPress site and made targeted improvements to '
            'it.\n\n'
            'The specific improvements are not itemised here. Listing '
            'them publicly requires confirming exactly which changes '
            'Aspired made and may describe; see the Denis Law Group rows '
            'in docs/brand_fact_matrix.md.'
        ),
        'results': (
            'This is a maintenance and improvement engagement on a site '
            'Aspired did not build, so there is no before-and-after '
            'build comparison to report and none is claimed.\n\n'
            'No performance figures are published for this engagement '
            'until their source, measurement window, baseline and client '
            'approval are documented.'
        ),
    },
    {
        'slug': 'food-trucks-of-san-antonio',
        'engagement_type': 'built',
        'title': 'Food Trucks of San Antonio',
        'business_type': 'Food & Events',
        'location': 'San Antonio, TX',
        'live_url': 'https://foodtrucksofsa.com/',
        'card_gradient': 'gradient-rust',
        'summary': (
            'A mobile-first directory for San Antonio’s food truck '
            'community — event listings, truck profiles and a location '
            'finder.'
        ),
        'challenge': (
            'Food truck customers are almost always on a phone, often '
            'outdoors, often on patchy mobile data, and deciding where '
            'to eat in the next few minutes. A site that takes several '
            'seconds to load has already lost them.\n\n'
            'The content also changes constantly — trucks move, events '
            'come and go — so the site had to stay easy to update '
            'without a developer involved every time.'
        ),
        'solution': (
            'A lean, mobile-first build with the weight kept deliberately '
            'low so it holds up on a phone away from wifi.\n\n'
            'Truck profiles, event listings and a location finder are '
            'structured as real content rather than a static page, so '
            'the directory can grow without the layout fighting it.'
        ),
        'results': (
            'A directory the community can actually use on a phone, '
            'built to stay fast as more trucks and events are added.'
        ),
    },
    {
        'slug': 'moonieful-designs',
        'engagement_type': 'built',
        'title': 'Moonieful Designs',
        'business_type': 'Creative Studio',
        'location': 'San Antonio, TX',
        'live_url': 'https://moonieful.com/',
        'card_gradient': 'gradient-purple',
        'summary': (
            'A portfolio site for a creative studio where the work has '
            'to be the loudest thing on the page.'
        ),
        'challenge': (
            'A design studio’s website is itself a portfolio piece. '
            'If the site looks templated, the work looks templated — '
            'no matter how good it is.\n\n'
            'The difficulty is restraint: the site has to be visually '
            'confident without competing with the projects it exists '
            'to display.'
        ),
        'solution': (
            'A clean, deliberately quiet build that puts the project '
            'showcase first and keeps browsing frictionless, with a '
            'direct path for a prospective client to get in touch '
            'rather than a buried contact page.'
        ),
        'results': (
            'A portfolio that presents the studio’s work without '
            'talking over it — and an ongoing working relationship '
            'between the two studios.'
        ),
    },
    {
        'slug': 'burgland-technologies',
        'engagement_type': 'built',
        'title': 'Burgland Technologies',
        'business_type': 'Technology',
        'location': '',
        'live_url': 'https://burglandtech.com/',
        'card_gradient': 'gradient-forest',
        'summary': (
            'A credibility-first site for a technology company, built '
            'to a standard a technical audience would actually inspect.'
        ),
        'challenge': (
            'Selling technology to people who understand technology '
            'sets a higher bar. A slow site, a broken layout or a '
            'missing security header is not a cosmetic problem to that '
            'audience — it is evidence.'
        ),
        'solution': (
            'A hand-coded, security-hardened build: HTTPS with strict '
            'transport security, hardened headers, a real content '
            'security policy, and clean semantic markup that stands up '
            'to someone opening developer tools.'
        ),
        'results': (
            'A site whose technical execution supports the claim the '
            'business is making, rather than quietly undermining it.'
        ),
    },
]


class Command(BaseCommand):
    help = 'Seed or refresh the four public portfolio case studies.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Overwrite existing rows, discarding any manual edits.')

    def handle(self, *args, **options):
        created = updated = skipped = 0

        for data in STUDIES:
            slug = data['slug']
            existing = CaseStudy.objects.filter(slug=slug).first()

            if existing and not options['force']:
                # Publish it if it somehow is not, but never clobber
                # copy that may have been edited in the admin.
                if not existing.is_published:
                    existing.is_published = True
                    existing.published_at = (existing.published_at
                                             or timezone.now())
                    existing.save()
                    updated += 1
                    self.stdout.write(f'  published: {slug}')
                else:
                    skipped += 1
                continue

            fields = dict(data)
            fields.pop('slug')
            fields['is_published'] = True

            if existing:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.published_at = existing.published_at or timezone.now()
                existing.save()
                updated += 1
                self.stdout.write(f'  overwritten: {slug}')
            else:
                CaseStudy.objects.create(
                    slug=slug, published_at=timezone.now(), **fields)
                created += 1
                self.stdout.write(f'  created: {slug}')

        self.stdout.write(self.style.SUCCESS(
            f'case studies — created {created}, updated {updated}, '
            f'unchanged {skipped}'))
