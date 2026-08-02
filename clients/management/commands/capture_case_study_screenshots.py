"""
Capture a screenshot of each published case study's live site.

Written as a management command rather than run once by hand because
client sites change. When one is redesigned, relaunched, or goes down,
this can be re-run to refresh the portfolio instead of someone
remembering how the images were made a year earlier.

Capture rules that matter:

  * 1440x900 viewport, above the fold only. The card visual is 16:10
    (.card--with-visual .card__visual), so the capture matches the slot
    and nothing gets cropped awkwardly. A full-page capture would be a
    tall strip that reads as a smear at card size.
  * Output is WebP at ~1200px wide. The portfolio page carries four of
    these and the site holds a 100 mobile performance score (§5.2) —
    unoptimised PNGs would spend that on decoration.
  * A site that does not return HTTP 200 is SKIPPED, not captured.
    Screenshotting an error page, a parked domain, or someone else's
    redesign would put a false claim on the portfolio.

Usage:
    python manage.py capture_case_study_screenshots
    python manage.py capture_case_study_screenshots --slug denis-law-group
    python manage.py capture_case_study_screenshots --force
"""

import asyncio
import io
import sys
import time

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from clients.models import CaseStudy

VIEWPORT = {'width': 1440, 'height': 900}   # 16:10, matches the card
OUTPUT_WIDTH = 1200                          # enough for a 2x card
WEBP_QUALITY = 82
SETTLE_SECONDS = 2.5                         # let fonts/hero animations land


class Command(BaseCommand):
    help = "Screenshot each published case study's live site into media/portfolio/."

    def add_arguments(self, parser):
        parser.add_argument(
            '--slug', help='Only this case study.')
        parser.add_argument(
            '--force', action='store_true',
            help='Re-capture even if a screenshot already exists.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be captured; write nothing.')

    def handle(self, *args, **opts):
        # Playwright launches its driver as a subprocess through
        # asyncio. On Windows only the Proactor loop can do that, and
        # inside a Django management command the policy has already been
        # set to Selector — so this raises NotImplementedError deep in
        # asyncio without the policy fixed first. Harmless elsewhere:
        # the branch only runs on win32, and this command is the only
        # thing in the process at the time.
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(
                asyncio.WindowsProactorEventLoopPolicy())

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CommandError(
                'Playwright is required: pip install playwright && '
                'playwright install chromium') from exc
        from PIL import Image

        studies = CaseStudy.objects.filter(is_published=True).exclude(
            live_url='')
        if opts['slug']:
            studies = studies.filter(slug=opts['slug'])
        # Evaluated NOW, before the browser starts. sync_playwright runs
        # its work inside a greenlet with a live asyncio loop, and
        # Django's async_unsafe guard rejects any ORM call made from
        # there (SynchronousOnlyOperation). So the rule for this command
        # is: touch the database before or after the browser block,
        # never inside it. Images are collected in memory and written
        # once the browser is closed.
        studies = list(studies)
        if not studies:
            raise CommandError('No published case studies with a live_url.')

        captured = skipped = failed = 0
        pending = []   # (study, webp_bytes, width, height)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            for study in studies:
                if study.screenshot and not opts['force']:
                    self.stdout.write(
                        f'  = {study.slug}: has one already (--force to '
                        f'replace)')
                    skipped += 1
                    continue

                page = browser.new_page(
                    viewport=VIEWPORT, device_scale_factor=2)
                try:
                    resp = page.goto(study.live_url, wait_until='networkidle',
                                     timeout=45000)
                    status = resp.status if resp else 0
                    if status != 200:
                        # Never publish a picture of a broken site.
                        self.stdout.write(self.style.WARNING(
                            f'  ! {study.slug}: HTTP {status} from '
                            f'{study.live_url} — skipped, keeping the '
                            f'gradient'))
                        skipped += 1
                        continue

                    time.sleep(SETTLE_SECONDS)
                    # Dismiss the usual cookie/consent overlays so they
                    # don't end up being the screenshot.
                    page.evaluate("""() => {
                        const pat = /cookie|consent|gdpr/i;
                        document.querySelectorAll(
                            'div,section,aside').forEach(el => {
                            const cs = getComputedStyle(el);
                            if ((cs.position === 'fixed'
                                 || cs.position === 'sticky')
                                && pat.test(el.className + ' ' + el.id)) {
                                el.style.display = 'none';
                            }
                        });
                    }""")
                    raw = page.screenshot(type='png')
                except Exception as exc:   # noqa: BLE001 — one bad site
                    self.stdout.write(self.style.WARNING(
                        f'  ! {study.slug}: {type(exc).__name__} — skipped'))
                    failed += 1
                    continue
                finally:
                    page.close()

                img = Image.open(io.BytesIO(raw)).convert('RGB')
                height = round(img.height * (OUTPUT_WIDTH / img.width))
                img = img.resize((OUTPUT_WIDTH, height), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, 'WEBP', quality=WEBP_QUALITY, method=6)
                pending.append((study, buf.getvalue(), OUTPUT_WIDTH, height))

            browser.close()

        # Browser is closed and the asyncio loop is gone — safe to
        # touch the ORM again.
        for study, data, width, height in pending:
            size_kb = len(data) / 1024
            if opts['dry_run']:
                self.stdout.write(
                    f'  ~ {study.slug}: would write {size_kb:.0f} KB '
                    f'({width}x{height})')
                continue
            study.screenshot.save(
                f'{study.slug}.webp', ContentFile(data), save=True)
            self.stdout.write(self.style.SUCCESS(
                f'  + {study.slug}: {size_kb:.0f} KB '
                f'({width}x{height}) from {study.live_url}'))
            captured += 1

        self.stdout.write(
            f'\nscreenshots — captured {captured}, skipped {skipped}, '
            f'failed {failed}')
