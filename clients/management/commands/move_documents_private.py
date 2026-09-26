"""
Move every ClientDocument file out of the public MEDIA_ROOT into
PRIVATE_MEDIA_ROOT.

ClientDocument.file now uses private storage (clients/storage.py), so a
file saved before that change still sits under MEDIA_ROOT — publicly
served by Nginx at /media/ — while the model looks for it under
PRIVATE_MEDIA_ROOT and 404s the download. This moves each one across,
keeping the same relative name so no database row changes.

Idempotent: a file already in private storage is skipped. The public copy
is only deleted after the private copy exists with the same size.

    python manage.py move_documents_private --dry-run
    python manage.py move_documents_private
"""

import os
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand

from clients.models import ClientDocument


class Command(BaseCommand):
    help = 'Move ClientDocument files from public MEDIA_ROOT into PRIVATE_MEDIA_ROOT.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would move without touching anything.')

    def handle(self, *args, dry_run=False, **options):
        public_root = os.path.abspath(str(settings.MEDIA_ROOT))
        private_root = os.path.abspath(str(settings.PRIVATE_MEDIA_ROOT))
        if public_root == private_root:
            self.stderr.write('PRIVATE_MEDIA_ROOT is the same as MEDIA_ROOT — refusing.')
            return

        moved = already = missing = failed = 0
        for doc in ClientDocument.objects.exclude(file='').iterator():
            name = doc.file.name
            src = os.path.abspath(os.path.join(public_root, name))
            dst = os.path.abspath(os.path.join(private_root, name))
            # Never follow a stored name outside either root.
            if (not src.startswith(public_root + os.sep)
                    or not dst.startswith(private_root + os.sep)):
                self.stderr.write(f'  skip (unsafe path): {name}')
                failed += 1
                continue

            if os.path.exists(dst):
                if os.path.exists(src) and not dry_run and \
                        os.path.getsize(src) == os.path.getsize(dst):
                    os.remove(src)
                already += 1
                continue
            if not os.path.exists(src):
                self.stderr.write(f'  missing on disk: {name} (document {doc.pk})')
                missing += 1
                continue

            if dry_run:
                self.stdout.write(f'  would move: {name}')
                moved += 1
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                if os.path.getsize(src) != os.path.getsize(dst):
                    raise OSError('size mismatch after copy')
                os.remove(src)
                moved += 1
            except OSError as exc:
                self.stderr.write(f'  FAILED {name}: {exc}')
                failed += 1

        verb = 'Would move' if dry_run else 'Moved'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {moved}; already private {already}; '
            f'missing {missing}; failed {failed}.'))
