"""
Point each case study at its screenshot file in MEDIA_ROOT/portfolio/.

Capturing and attaching are separate jobs because they happen in
different places. `capture_case_study_screenshots` drives a headless
browser, which the servers do not have installed — screenshots are
taken on a workstation and the resulting files are copied up. This
command is the other half: it runs on the server, finds the files that
were copied, and sets the database pointers.

Idempotent, and safe to leave in a deploy script. Matches
`portfolio/<slug>.webp` by slug, skips a study whose field is already
correct, and never invents a row.
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand

from clients.models import CaseStudy

SUBDIR = 'portfolio'


class Command(BaseCommand):
    help = ('Attach media/portfolio/<slug>.webp files to their case '
            'studies. Run after copying screenshots to a server.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change; write nothing.')

    def handle(self, *args, **opts):
        attached = unchanged = missing = 0

        for study in CaseStudy.objects.exclude(slug=''):
            rel = f'{SUBDIR}/{study.slug}.webp'
            abs_path = os.path.join(settings.MEDIA_ROOT, SUBDIR,
                                    f'{study.slug}.webp')

            if not os.path.exists(abs_path):
                # Not an error. A study may legitimately have no
                # screenshot — an offline site, or one that was never
                # public — and the card falls back to its gradient.
                if study.screenshot:
                    self.stdout.write(self.style.WARNING(
                        f'  ! {study.slug}: DB points at '
                        f'{study.screenshot.name} but no file is there — '
                        f'the card will render a broken image'))
                missing += 1
                continue

            if study.screenshot and study.screenshot.name == rel:
                unchanged += 1
                continue

            size_kb = os.path.getsize(abs_path) / 1024
            if opts['dry_run']:
                self.stdout.write(
                    f'  ~ {study.slug}: would attach {rel} '
                    f'({size_kb:.0f} KB)')
                continue

            # Assign the relative name directly rather than re-saving
            # the bytes through the storage backend — the file is
            # already in place, and .save() would copy it to
            # <slug>_<random>.webp and orphan the original.
            study.screenshot.name = rel
            study.save(update_fields=['screenshot', 'updated_at'])
            self.stdout.write(self.style.SUCCESS(
                f'  + {study.slug}: {rel} ({size_kb:.0f} KB)'))
            attached += 1

        self.stdout.write(
            f'\nscreenshots — attached {attached}, unchanged {unchanged}, '
            f'no file {missing}')
