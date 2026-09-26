"""
Content gate (Sept 2026 implementation plan §12.1).

    python manage.py content_gate                      # render locally
    python manage.py content_gate --base-url https://staging.aspiredwebsites.com
    python manage.py content_gate --base-url https://aspiredwebsites.com

Exits non-zero if any public page contains forbidden wording. See
public/content_gate.py for the rules and the whitelist.
"""

from django.core.management.base import BaseCommand, CommandError

from public import content_gate


class Command(BaseCommand):
    help = 'Scan every public page for wording the site must never ship.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--base-url', default='',
            help='Scan a live site instead of rendering locally.')

    def handle(self, *args, **options):
        base = options['base_url'].rstrip('/')
        paths = content_gate.all_paths()
        hits, failed = [], []

        if base:
            import requests
            for path in paths:
                try:
                    r = requests.get(base + path, timeout=30,
                                     headers={'User-Agent': 'aspired-content-gate'})
                except requests.RequestException as exc:
                    failed.append(f'{path}: {exc}')
                    continue
                if r.status_code != 200:
                    failed.append(f'{path}: HTTP {r.status_code}')
                    continue
                hits.extend(content_gate.scan(path, r.text))
        else:
            from django.test import Client
            from django.test.utils import setup_test_environment
            setup_test_environment()
            client = Client()
            for path in paths:
                r = client.get(path, HTTP_HOST='aspiredwebsites.com', secure=True)
                if r.status_code != 200:
                    failed.append(f'{path}: HTTP {r.status_code}')
                    continue
                hits.extend(content_gate.scan(path, r.content.decode('utf-8')))

        self.stdout.write(f'Scanned {len(paths)} pages.')
        for line in failed:
            self.stdout.write(self.style.WARNING(f'  not scanned: {line}'))
        for hit in hits:
            self.stdout.write(self.style.ERROR(f'  {hit}'))
        if hits or failed:
            raise CommandError(
                f'content gate failed: {len(hits)} forbidden phrase(s), '
                f'{len(failed)} page(s) not scanned')
        self.stdout.write(self.style.SUCCESS('content gate passed'))
