"""
Write the 600 px copy of every case-study desktop screenshot, used by the
portfolio card's srcset (see clients/screenshot_variants.py).

Idempotent: existing copies are skipped unless --force. Safe on every
deploy; capture_case_study_screenshots also calls it for new captures.

    python manage.py make_screenshot_variants
    python manage.py make_screenshot_variants --force
"""
from django.core.management.base import BaseCommand

from clients.models import CaseStudy
from clients.screenshot_variants import make_variant


class Command(BaseCommand):
    help = 'Create the 600 px srcset copy of each case-study screenshot.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Rewrite copies that already exist.')

    def handle(self, *args, **opts):
        written = skipped = failed = 0
        for study in CaseStudy.objects.exclude(screenshot=''):
            try:
                if make_variant(study.screenshot, force=opts['force']):
                    written += 1
                    self.stdout.write(f'  + {study.slug}')
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 (one missing file must not stop the rest)
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f'  ! {study.slug}: {type(exc).__name__}: {exc}'))
        self.stdout.write(
            f'screenshot variants: written {written}, skipped {skipped}, '
            f'failed {failed}')
