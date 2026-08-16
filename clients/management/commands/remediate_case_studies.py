"""
Correct published case-study copy that misstates what Aspired did.

`seed_case_studies` cannot do this job. Without `--force` it deliberately
never touches an existing row (so admin edits survive), and with `--force`
it overwrites every field of every study — discarding real edits to fix one
of them. Neither is an acceptable production data correction.

This command targets only the specific untrue statements, leaves everything
else alone, and reports exactly what it would change before changing it.

The Denis Law Group correction (approved in `docs/brand_fact_matrix.md`):
Aspired did NOT build that website. It is an existing WordPress site that
Aspired maintains and has improved. Any published row still describing a
build is a false public claim about a real client.

Nothing is invented. The reported ~2-3 contacts per week is NOT written
here — its source, measurement window, baseline, attribution and client
approval are unproven, so the correct action is to publish no figure.

    python manage.py remediate_case_studies              # dry run
    python manage.py remediate_case_studies --apply
"""

from django.core.management.base import BaseCommand

from clients.management.commands.seed_case_studies import STUDIES


# Phrases that assert Aspired built a site. Presence in a study whose
# engagement type is not 'built' is a misrepresentation.
BUILD_CLAIM_MARKERS = (
    'built from scratch',
    'hand-coded',
    'hand coded',
    'no template',
    'no page builder',
    'new practice launch',
    'we built',
    'built by aspired',
)

# Studies whose public copy is corrected by this command, keyed by slug.
# Values are the fields to force onto the row. Sourced from the seed so
# there is one definition of the corrected copy, not two.
REMEDIATIONS = {
    'denis-law-group': (
        'Aspired did not build this site; it is an existing WordPress '
        'site Aspired maintains and improves.'),
}


class Command(BaseCommand):
    help = ('Correct published case studies that misstate Aspired\'s '
            'relationship to the client site. Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the corrections (default: report only).')
        parser.add_argument(
            '--slug', type=str, default='',
            help='Limit to one case study slug.')

    def handle(self, *args, **opts):
        from clients.models import CaseStudy

        apply = opts['apply']
        only = opts['slug']
        seed_by_slug = {item['slug']: item for item in STUDIES}

        self.stdout.write('DRY RUN - no writes\n' if not apply
                          else 'APPLYING corrections\n')

        changed = 0
        for slug, reason in REMEDIATIONS.items():
            if only and slug != only:
                continue
            study = CaseStudy.objects.filter(slug=slug).first()
            if study is None:
                self.stdout.write(
                    f'  {slug}: no row in this database — nothing to fix')
                continue

            corrected = seed_by_slug.get(slug)
            if corrected is None:
                self.stdout.write(self.style.WARNING(
                    f'  {slug}: no corrected copy defined; skipped'))
                continue

            fields = {
                key: value for key, value in corrected.items()
                if key != 'slug'
            }
            diffs = [
                key for key, value in fields.items()
                if getattr(study, key, None) != value
            ]
            if not diffs:
                self.stdout.write(f'  {slug}: already correct')
                continue

            changed += 1
            self.stdout.write(f'  {slug}: {reason}')
            for key in diffs:
                before = str(getattr(study, key, '') or '')[:70]
                after = str(fields[key] or '')[:70]
                self.stdout.write(f'      {key}:')
                self.stdout.write(f'        was: {before}')
                self.stdout.write(f'        now: {after}')
            if apply:
                for key in diffs:
                    setattr(study, key, fields[key])
                study.save(update_fields=diffs + ['updated_at'])

        # Anything still asserting a build that is not tagged as one.
        self.stdout.write('')
        self.stdout.write('Residual build claims:')
        residual = 0
        for study in CaseStudy.objects.all():
            if study.engagement_type == 'built':
                continue
            haystack = ' '.join(filter(None, [
                study.summary, study.challenge, study.solution,
                study.results,
            ])).lower()
            hits = [m for m in BUILD_CLAIM_MARKERS if m in haystack]
            if hits:
                residual += 1
                self.stdout.write(self.style.WARNING(
                    f'  {study.slug} (engagement_type='
                    f'{study.engagement_type or "unset"!r}): {", ".join(hits)}'))
        if not residual:
            self.stdout.write('  none')

        self.stdout.write('')
        self.stdout.write(
            f'Studies {"corrected" if apply else "needing correction"}: '
            f'{changed}')
        if not apply and changed:
            self.stdout.write(self.style.WARNING(
                'Re-run with --apply to write these corrections.'))
