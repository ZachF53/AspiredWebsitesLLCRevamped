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

  * Denis Law Group was a NEW practice launch. There is no "before"
    traffic to compare against, so the study runs on architecture and
    decisions rather than a made-up percentage. Zach confirmed this
    directly (2026-08-02).

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
        'summary': (
            'A custom, security-hardened website for a growing law '
            'practice — built from scratch, structured for local search, '
            'and owned outright by the firm.'
        ),
        'challenge': (
            'A new practice needs a website that reads as established '
            'from the first visit. Prospective clients compare several '
            'firms before calling one, and most of that judgement '
            'happens in the first few seconds — before anyone reads a '
            'word about experience.\n\n'
            'The site also had to be built so it could grow. A law firm '
            'that adds a practice area should be able to add a page for '
            'it, not rewrite the site.'
        ),
        'solution': (
            'A hand-coded site with no template and no page builder, '
            'built mobile-first because that is where most legal '
            'searches start.\n\n'
            'The structure is the important part: practice areas are '
            'built as their own pages rather than a single combined '
            'list, so each one can be the best answer to its own '
            'search and new areas slot in without restructuring.\n\n'
            'Intake is treated as sensitive from the start — contact '
            'submissions carry facts about a legal problem before any '
            'engagement letter exists, so the form is validated '
            'server-side, rate-limited, and delivered straight to the '
            'firm rather than pooled on a server.'
        ),
        'results': (
            'The firm owns every file: source code, content and domain. '
            'There is no platform licence and nothing to renew for the '
            'site to keep working.\n\n'
            'An honest note on numbers: this was a new practice launch, '
            'so there is no "before" traffic to compare against and we '
            'will not invent one. For clients replacing an existing '
            'site we capture speed, indexed pages, rankings and lead '
            'volume before launch and re-measure at 30 and 90 days.'
        ),
    },
    {
        'slug': 'food-trucks-of-san-antonio',
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
