"""
Create the Texas Law campaign - locally, and optionally in Instantly.

    # 1. Preview the copy. Touches nothing.
    python manage.py seed_outreach_campaign --dry-run \
        --postal-address "123 Main St, San Antonio, TX 78205"

    # 2. Create the Django-side campaign row.
    python manage.py seed_outreach_campaign \
        --postal-address "123 Main St, San Antonio, TX 78205"

    # 3. Create it in Instantly too, PAUSED.
    python manage.py seed_outreach_campaign --create-in-instantly \
        --postal-address "123 Main St, San Antonio, TX 78205"

Step 3 writes to the live Instantly workspace, which is why it is a
separate opt-in flag rather than the default. Even then the campaign is
created paused and this command offers no way to start it - putting mail
in front of real people stays a deliberate click in Instantly's UI.

The Django campaign is created with ``active=False`` for the same
reason: a campaign that arrived pushable by default could start
receiving leads before anyone had read the copy.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from outreach import sequences
from outreach.models import OutreachCampaign


class Command(BaseCommand):
    help = 'Create an outreach campaign and its sequence copy.'

    def add_arguments(self, parser):
        parser.add_argument('--sequence', default='texas-law',
                            help='Which sequence in outreach/sequences.py.')
        parser.add_argument(
            '--offer', default='',
            help='One offer key, or "all" to seed one campaign per offer.')
        parser.add_argument('--name', default='TX - Law Firms')
        parser.add_argument('--niche', default='law firm')
        parser.add_argument('--state', default='TX')
        parser.add_argument('--city', default='')
        parser.add_argument('--business-type', default='Law Firm')
        parser.add_argument(
            '--postal-address', default='',
            help='CAN-SPAM requires this in every commercial email.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print the copy and the pre-flight result. Write nothing.')
        parser.add_argument(
            '--create-in-instantly', action='store_true',
            help='Also create the campaign in Instantly, PAUSED.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok = self.style.SUCCESS
        bad = self.style.ERROR
        warn = self.style.WARNING

        offer = opts['offer'] or sequences.DEFAULT_OFFER
        if offer == 'all':
            self._seed_all(opts)
            return
        try:
            steps = sequences.build_steps(
                opts['sequence'], opts['postal_address'], offer=offer)
        except sequences.SequenceError as exc:
            raise CommandError(str(exc))

        # ── Pre-flight ────────────────────────────────────────────────
        problems = sequences.describe_problems(steps)
        w(self.style.MIGRATE_HEADING(
            f"\n=== SEQUENCE: {opts['sequence']} ({len(steps)} touches) ===\n"))

        for i, step in enumerate(steps, 1):
            delay = step['delay_days']
            when = 'immediately' if i == 1 else f'{delay} days after touch {i-1}'
            subject = step['subject'] or '(blank - threads under previous)'
            w(self.style.MIGRATE_LABEL(f'--- Touch {i} - sent {when}'))
            w(f'Subject: {subject}')
            w('')
            for line in step['body'].splitlines():
                w(f'  {line}')
            w(f'\n[{len(step["body"].split())} words]\n')

        if problems:
            w(bad('\nPRE-FLIGHT FAILED:'))
            for p in problems:
                w(bad(f'  X {p}'))
            raise CommandError('Copy is not fit to send. Fix and re-run.')
        w(ok('Pre-flight passed: length, plain text, footer, no pricing.\n'))

        if opts['dry_run']:
            w(warn('--dry-run: nothing written.'))
            return

        # ── Django side ───────────────────────────────────────────────
        slug = slugify(opts['name']) or opts['sequence']
        campaign, created = OutreachCampaign.objects.get_or_create(
            slug=slug,
            defaults={
                'name': opts['name'],
                'niche': opts['niche'],
                'business_type': opts['business_type'],
                'city': opts['city'],
                'state': opts['state'],
                'active': False,
            },
        )
        verb = 'Created' if created else 'Found existing'
        w(ok(f'{verb} campaign: {campaign.name} (slug={campaign.slug})'))

        # ── Instantly side ────────────────────────────────────────────
        if opts['create_in_instantly']:
            from outreach import instantly

            if campaign.instantly_campaign_id:
                w(warn(f'Already linked to Instantly campaign '
                       f'{campaign.instantly_campaign_id} - not creating '
                       f'a second one.'))
            else:
                try:
                    result = instantly.create_campaign(
                        name=opts['name'], sequence_steps=steps)
                except instantly.InstantlyError as exc:
                    raise CommandError(f'Instantly refused: {exc}')
                campaign.instantly_campaign_id = str(result.get('id') or '')
                campaign.save(update_fields=[
                    'instantly_campaign_id', 'updated_at'])
                w(ok(f'Created in Instantly, PAUSED: '
                     f'{campaign.instantly_campaign_id}'))
        else:
            w(warn('\nNot created in Instantly (pass --create-in-instantly).'))
            w('Or create it by hand and paste the id into '
              '/admin/outreach/outreachcampaign/')

        # ── What still blocks a send ──────────────────────────────────
        w(self.style.MIGRATE_HEADING('\n=== STILL BLOCKING A REAL SEND ==='))
        blockers = []
        if not campaign.instantly_campaign_id:
            blockers.append('Campaign has no instantly_campaign_id.')
        if not campaign.active:
            blockers.append(
                f'Campaign is paused. Activate at '
                f'/admin/outreach/outreachcampaign/{campaign.pk}/change/ '
                f'once you have read the copy.')
        from django.conf import settings
        if not getattr(settings, 'EMAIL_VERIFY_PROVIDER', ''):
            blockers.append(
                'EMAIL_VERIFY_PROVIDER unset - every lead stays '
                '"unverified", which is not sendable.')
        # Deliberately NOT checking INSTANTLY_WEBHOOK_SECRET. Webhooks
        # need a higher Instantly plan; replies arrive by polling the
        # unibox instead (poll_instantly_replies_task), which needs no
        # secret. Flagging the unset secret would report a blocker that
        # is not one.
        blockers.append(
            'The 9 *aspiredwebsites.com mailboxes are setup_pending - '
            'connect them, then warmup runs 2-3 weeks.')

        for b in blockers:
            w(bad(f'  X {b}'))
        w('')

    def _seed_all(self, opts):
        """One campaign per offer -- the A/B/C/D/E/F harness.

        Separate campaigns rather than Instantly's in-campaign variants
        because per-campaign analytics is what already exists, and a
        reply rate is only interpretable if exactly one thing differs
        between arms. Here that one thing is the offer.
        """
        w = self.stdout.write
        for key, spec in sequences.OFFERS.items():
            try:
                steps = sequences.build_steps(
                    opts['sequence'], opts['postal_address'], offer=key)
            except sequences.SequenceError as exc:
                raise CommandError(str(exc))
            problems = sequences.describe_problems(steps)
            if problems:
                raise CommandError(f'{key}: {problems}')

            slug = slugify(f"{opts['name']}-{key}")
            campaign, created = OutreachCampaign.objects.get_or_create(
                slug=slug,
                defaults={
                    'name': f"{opts['name']} [{spec['name']}]",
                    'niche': opts['niche'],
                    'business_type': opts['business_type'],
                    'city': opts['city'],
                    'state': opts['state'],
                    'active': False,
                },
            )
            verb = 'created' if created else 'exists'
            w(f"  {verb:8} {slug:44} {spec['fulfilment_cost'][:44]}")
        w('')
        w(self.style.WARNING(
            'All paused, none linked to Instantly yet. Create each in '
            'Instantly and paste its id in the admin.'))
        w(self.style.WARNING(
            'Read the sample-size note before splitting leads six ways.'))
