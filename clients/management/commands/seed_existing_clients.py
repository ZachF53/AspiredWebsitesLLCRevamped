"""
seed_existing_clients — pre-platform clients carried over from before
the platform existed.

Idempotent — `update_or_create` keyed on `firm_name`, so re-running
overwrites stale fields without spawning duplicates. Each client gets a
ClientProfile, a Project at stage='live', and an empty ClientVault.
SSH vault keys are NOT installed here — run `setup_vault_keys_for_existing`
afterwards for that step.

These are real clients with existing Droplets and no maintenance plans.
The legacy-source breadcrumb lives in `internal_notes` so the
onboarding-status page can pick them up later.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from clients.models import ClientProfile, Project
from vault.models import ClientVault

User = get_user_model()

MOONIEFUL_DROPLET = 'MoonieFull-Designs'

CLIENTS = [
    {
        'firm_name': 'Bermea Wedding',
        'droplet_name': 'bermeawedding2026',
        'do_droplet_ip': '24.144.99.254',
        'business_type': 'Wedding Photography',
        'package': 'essential_build',
        'contact_name': '',
        'email': '',
        'city': '',
        'state': '',
        'live_url': '',
        'is_tester': False,
    },
    {
        'firm_name': 'Aspired AI',
        'droplet_name': 'aspired-ai',
        'do_droplet_ip': '174.138.61.191',
        'business_type': 'Technology',
        'package': 'essential_build',
        'contact_name': 'Zachery Long',
        'email': 'zacherylong@aspiredwebsites.com',
        'city': 'San Antonio',
        'state': 'TX',
        'live_url': '',
        'is_tester': True,
    },
    {
        'firm_name': 'Aspired N8N Automation',
        'droplet_name': 'aspired-n8n-automation',
        'do_droplet_ip': '104.236.103.73',
        'business_type': 'Technology',
        'package': 'essential_build',
        'contact_name': 'Zachery Long',
        'email': '',
        'city': 'San Antonio',
        'state': 'TX',
        'live_url': '',
        'is_tester': True,
    },
    {
        'firm_name': 'SSG Education',
        'droplet_name': 'ssg-education',
        'do_droplet_ip': '104.236.36.55',
        'business_type': 'Education',
        'package': 'essential_build',
        'contact_name': '',
        'email': '',
        'city': '',
        'state': '',
        'live_url': '',
        'is_tester': False,
    },
    {
        'firm_name': 'Anita Vople Skin Care',
        'droplet_name': 'Anita-Vople-Skin-Care',
        'do_droplet_ip': '174.138.83.198',
        'business_type': 'Beauty & Skincare',
        'package': 'essential_build',
        'contact_name': 'Anita Vople',
        'email': '',
        'city': '',
        'state': '',
        'live_url': '',
        'is_tester': False,
    },
    {
        'firm_name': 'Burgland Technology',
        'droplet_name': 'Burgland-Technology-Consultant',
        'do_droplet_ip': '161.35.128.56',
        'business_type': 'Technology',
        'package': 'essential_build',
        'contact_name': '',
        'email': '',
        'city': '',
        'state': '',
        'live_url': '',
        'is_tester': False,
    },
    {
        'firm_name': 'Rachael Link Tree',
        'droplet_name': 'Rachael-Link-Tree',
        'do_droplet_ip': '159.65.181.23',
        'business_type': 'Personal Brand',
        'package': 'essential_build',
        'contact_name': 'Rachael',
        'email': '',
        'city': '',
        'state': '',
        'live_url': '',
        'is_tester': False,
    },
    {
        'firm_name': 'Rachael Drayton Blog',
        'droplet_name': 'Rachael-Drayton-Blog',
        'do_droplet_ip': '167.71.106.162',
        'business_type': 'Blog',
        'package': 'essential_build',
        'contact_name': 'Rachael Drayton',
        'email': '',
        'city': '',
        'state': '',
        'live_url': '',
        'is_tester': False,
    },
    {
        'firm_name': 'Moonieful Designs',
        'droplet_name': MOONIEFUL_DROPLET,
        'do_droplet_ip': '161.35.118.69',
        'business_type': 'Design Studio',
        'package': 'moonieful_referred',
        'contact_name': 'Miki Roberts',
        'email': '',
        'city': 'Atlanta',
        'state': 'GA',
        'live_url': 'moonieful.com',
        'is_tester': False,
    },
    {
        'firm_name': 'Food Trucks of San Antonio',
        'droplet_name': 'foodtrucks2021',
        'do_droplet_ip': '143.244.155.250',
        'business_type': 'Food & Events',
        'package': 'essential_build',
        'contact_name': 'Zachery Long',
        'email': 'zacherylong@aspiredwebsites.com',
        'city': 'San Antonio',
        'state': 'TX',
        'live_url': '',
        'is_tester': True,
    },
]


class Command(BaseCommand):
    help = 'Seed legacy clients (pre-platform) with profile, project, vault.'

    def handle(self, *args, **options):
        for data in CLIENTS:
            self._seed_one(data)
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done — {len(CLIENTS)} legacy client(s) processed.'))
        self.stdout.write(
            'Next: run `python manage.py setup_vault_keys_for_existing` '
            'to install SSH vault keys.')

    # ── per-client helpers ──────────────────────────────────────────────────

    def _seed_one(self, data):
        user = self._get_or_create_user(data)
        is_moonieful = data['droplet_name'] == MOONIEFUL_DROPLET
        notes = (
            f"Legacy client — built before platform launch.\n"
            f"DO Droplet: {data['droplet_name']}\n"
            f"No maintenance plan.\n"
            f"Tester: {data['is_tester']}"
        )

        profile, created = ClientProfile.objects.update_or_create(
            firm_name=data['firm_name'],
            defaults={
                'user': user,
                'contact_name': data['contact_name'],
                'business_type': data['business_type'],
                'package': data['package'],
                'city': data['city'],
                'state': data['state'],
                'do_droplet_ip': data['do_droplet_ip'],
                'status': 'active',
                'onboarding_complete': True,
                'maintenance_active': False,
                'synced_from_moonieful': is_moonieful,
                'internal_notes': notes,
            },
        )

        Project.objects.update_or_create(
            client=profile,
            defaults={
                'stage': 'live',
                'package': (data['package']
                            if data['package'] in {'essential_build',
                                                   'premium_build'}
                            else 'essential_build'),
                'live_url': data['live_url'],
                'payment_status': 'fully_paid',
                'moonieful_referred': is_moonieful,
            },
        )

        ClientVault.objects.get_or_create(client=profile)

        action = 'created' if created else 'updated'
        if user and user.email:
            email_line = user.email
            if not user.is_active:
                email_line += ' (placeholder — collides with another profile)'
        else:
            email_line = 'NO EMAIL — add later (placeholder user)'
        self.stdout.write(
            f"  ✓ {data['firm_name']} — {action}")
        self.stdout.write(f"    IP: {data['do_droplet_ip']}")
        self.stdout.write(f"    User: {email_line}")
        self.stdout.write(f"    Tester: {data['is_tester']}")

    def _get_or_create_user(self, data):
        """
        ClientProfile.user is a non-null OneToOneField, so every client
        needs its own Django user.

        Idempotent — if a ClientProfile with this firm_name already
        exists, reuse its user (so the seed can run twice without
        reshuffling). Otherwise:

          - With an email, reuse an existing User with that email IF
            that user isn't already attached to another ClientProfile
            (the OneToOne would refuse). Otherwise fall through to a
            placeholder so each profile gets its own user.
          - Without an email, create an inactive placeholder user with
            an unusable password — same pattern as vault.views.new_vault.
        """
        existing = ClientProfile.objects.filter(
            firm_name=data['firm_name']).select_related('user').first()
        if existing and existing.user:
            return existing.user

        email = (data.get('email') or '').strip()
        contact = (data.get('contact_name') or '').strip()
        first_name = contact.split(' ', 1)[0] if contact else ''

        if email:
            candidate = User.objects.filter(email=email).first()
            if candidate is None:
                return User.objects.create(
                    username=email, email=email,
                    first_name=first_name, is_active=True)
            # If the existing user is still unattached, link them here.
            if not ClientProfile.objects.filter(user=candidate).exists():
                return candidate
            # Already used — fall through to a uniquified placeholder so
            # the OneToOne constraint holds.

        # Placeholder — inactive, unusable password. Preserve the email
        # on the placeholder so it still surfaces on the onboarding card.
        base = 'legacy-' + (slugify(data['firm_name']) or 'client')
        username = base
        suffix = 1
        while User.objects.filter(username=username).exists():
            username = f'{base}-{suffix}'
            suffix += 1
        user = User(
            username=username, email=email,
            first_name=first_name, is_staff=False, is_active=False)
        user.set_unusable_password()
        user.save()
        return user
