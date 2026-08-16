"""
Build a synthetic, production-shaped dataset for the Account/Website
migration rehearsal.

The local database is empty, so a clean parity report proves nothing.  This
command creates the client shapes production actually contains — direct
build clients, a Moonieful referral, a multi-website account, subscription
-only accounts, contracts, payments, domains, droplets and vault
credentials — *including* the structural defects a real dataset carries:

  * ClientProfiles created before the autocreate signal existed, so they
    have no Account and their Projects have no Website.
  * An Account whose user drifted away from its legacy ClientProfile's user.
  * Dependent rows still carrying only their legacy ``client`` / ``project``
    FK.
  * A Website whose ``legacy_project`` belongs to a different Account.
  * Duplicate Stripe customer / subscription IDs and a duplicate DigitalOcean
    droplet ID.
  * Account and Website fields that drifted from the legacy row because a
    writer used ``queryset.update()`` and bypassed the sync signal.
  * A multi-website account whose historical client-level rows cannot be
    split by the "oldest website wins" rule.

Those defects are the point: the rehearsal has to *repair* them and then
prove ``audit_account_website_parity --strict --fail-on-warnings`` passes.

Safety: refuses to run against anything but a rehearsal database.

Usage::

    python manage.py seed_rehearsal_dataset --fresh \\
        --settings=AspiredWebsitesRevamped.settings_rehearsal
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


User = get_user_model()

# Stable UUID namespace so a re-seed produces the same identifiers and a
# report diff between two rehearsal runs is meaningful.
_NS = uuid.UUID('9f2a1c7e-0b45-4f1a-9c3d-6b7e8a0d1f22')


def _uid(label):
    return uuid.uuid5(_NS, label)


class Command(BaseCommand):
    help = ('Seed a production-shaped synthetic dataset (with realistic '
            'migration defects) for the Account/Website rehearsal.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--fresh', action='store_true',
            help='Delete existing client/account data before seeding.')
        parser.add_argument(
            '--force', action='store_true',
            help='Bypass the rehearsal-database safety check. Do not use '
                 'against a database you care about.')

    # ── Safety ──────────────────────────────────────────────────────────

    def _assert_rehearsal_db(self, force):
        name = str(settings.DATABASES['default'].get('NAME', ''))
        engine = settings.DATABASES['default'].get('ENGINE', '')
        is_rehearsal = (
            'rehearsal' in name.lower()
            and 'sqlite' in engine
        )
        if is_rehearsal or force:
            if not is_rehearsal:
                self.stdout.write(self.style.WARNING(
                    f'--force given; seeding into {name}'))
            return
        raise CommandError(
            'Refusing to seed: this is not a rehearsal database '
            f'(ENGINE={engine}, NAME={name}). Run with '
            '--settings=AspiredWebsitesRevamped.settings_rehearsal.')

    # ── Entry point ─────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        self._assert_rehearsal_db(opts['force'])

        if opts['fresh']:
            self._flush()

        self.now = timezone.now()
        with transaction.atomic():
            self._seed()

        self._summary()

    def _flush(self):
        from clients.account_models import (
            Account, SubscriptionPaymentMethod, Website, WebsiteStageLog)
        from clients.models import ClientProfile
        from domains.models import DomainRegistration

        self.stdout.write('Flushing existing client data…')
        # DomainRegistration PROTECTs its ClientProfile — clear it first.
        DomainRegistration.objects.all().delete()
        SubscriptionPaymentMethod.objects.all().delete()
        WebsiteStageLog.objects.all().delete()
        Website.objects.all().delete()
        Account.objects.all().delete()
        ClientProfile.objects.all().delete()
        User.objects.filter(email__endswith='@rehearsal.invalid').delete()

    # ── Builders ────────────────────────────────────────────────────────

    def _user(self, handle, first='', last=''):
        user, _ = User.objects.get_or_create(
            username=handle,
            defaults={
                'email': f'{handle}@rehearsal.invalid',
                'first_name': first,
                'last_name': last,
            },
        )
        return user

    def _profile(self, *, handle, firm, skip_autocreate=False,
                 skip_website=False, **fields):
        """Create a ClientProfile through the normal path (signals fire)."""
        from clients.models import ClientProfile

        user = self._user(handle, *(fields.pop('person', ('', ''))))
        cp = ClientProfile(user=user, firm_name=firm, **fields)
        if skip_autocreate:
            cp._skip_autocreate = True
        if skip_website:
            cp._skip_website_autocreate = True
        cp.save()
        return cp

    def _project(self, cp, **fields):
        from clients.models import Project
        return Project.objects.create(client=cp, **fields)

    def _vault(self, cp, notes=''):
        """The vault app auto-creates a ClientVault on profile creation —
        reuse that row rather than colliding with its unique client FK."""
        from vault.models import ClientVault

        vault, _ = ClientVault.objects.get_or_create(client=cp)
        if notes and vault.notes != notes:
            vault.notes = notes
            vault.save(update_fields=['notes', 'updated_at'])
        return vault

    def _ssh_credential(self, vault, *, label, website=None,
                        server_key=True):
        from vault.crypto import derive_server_key, encrypt_value
        from vault.models import VaultCredential

        key = derive_server_key()
        return VaultCredential.objects.create(
            vault=vault,
            website_new=website,
            category='server',
            credential_type='ssh',
            label=label,
            is_ssh_credential=True,
            ssh_host_encrypted=encrypt_value('203.0.113.10', key),
            ssh_username_encrypted=encrypt_value('root', key),
            ssh_auth_type='private_key',
            ssh_private_key_encrypted=encrypt_value(
                'REHEARSAL-NOT-A-REAL-KEY', key),
            username_hint='roo***',
            encrypted_with_server_key=server_key,
        )

    # ── The dataset ─────────────────────────────────────────────────────

    def _seed(self):
        self._seed_direct_live_client()
        self._seed_moonieful_client()
        self._seed_multi_website_account()
        self._seed_pre_signal_orphan()
        self._seed_user_mismatch()
        self._seed_duplicate_identifiers()
        self._seed_subscription_only()
        self._seed_field_drift()
        self._seed_cross_account_project()

    # 1 ── Direct client, full lifecycle, live ──────────────────────────
    def _seed_direct_live_client(self):
        """Johnson Law — the happy path, end to end.

        Every dependent row is created with only its legacy FK, exactly as
        the pre-refactor writers left them.  The backfill has to repoint
        them.
        """
        from clients.models import (
            ClientDocument, Contract, ContractService, IntakeResponse,
            PaymentRecord, ProjectStageLog, RevisionRequest, SupportTicket)
        from clients.service_models import Droplet, MaintenancePlan
        from domains.models import DomainRegistration


        launched = (self.now - timedelta(days=95)).date()
        cp = self._profile(
            handle='johnsonlaw', firm='Johnson Law Firm',
            person=('Marcus', 'Johnson'),
            contact_name='Marcus Johnson', phone='210-555-0142',
            website='https://johnsonlawfirm.com',
            address='4400 Broadway St', city='San Antonio', state='TX',
            zip_code='78209', business_type='Law Firm',
            package='essential_build',
            stripe_customer_id='cus_REHEARSALJOHNSON',
            stripe_subscription_id='sub_REHEARSALJOHNSONMAINT',
            stripe_hosting_subscription_id='sub_REHEARSALJOHNSONHOST',
            stripe_invoice_id='in_REHEARSALJOHNSON',
            maintenance_active=True,
            maintenance_started_at=self.now - timedelta(days=80),
            onboarding_status='onboarding_complete',
            onboarding_complete=True,
            do_droplet_id='401234567',
            do_droplet_ip='203.0.113.10',
            do_droplet_created_at=self.now - timedelta(days=120),
            stage='live', payment_status='fully_paid',
            deposit_paid_at=self.now - timedelta(days=130),
            final_paid_at=self.now - timedelta(days=100),
            launch_date=launched,
            support_window_ends=launched + timedelta(days=14),
            revision_count=2,
        )
        project = self._project(
            cp, stage='live', package='essential_build',
            staging_url='https://staging.johnsonlawfirm.com',
            live_url='https://johnsonlawfirm.com',
            launch_date=launched,
            support_window_ends=launched + timedelta(days=14),
            payment_status='fully_paid',
            deposit_paid_at=self.now - timedelta(days=130),
            final_paid_at=self.now - timedelta(days=100),
            revision_count=2,
        )
        self.johnson_cp = cp
        self.johnson_project = project

        # Legacy-only dependent rows.
        IntakeResponse.objects.create(
            project=project, client=cp, completed=True,
            completed_at=self.now - timedelta(days=140),
            brand_colors='#1B3A5C, #C9A227', brand_fonts='Georgia, Arial',
            practice_areas='Personal Injury\nFamily Law',
            domain_name='johnsonlawfirm.com', domain_registrar='namecheap',
        )
        for note, delta in (('Kickoff', 150), ('Design approved', 135),
                            ('Launched', 95)):
            ProjectStageLog.objects.create(
                project=project, client=cp, from_stage='structure',
                to_stage='live', note=note, set_by='system',
                created_at=self.now - timedelta(days=delta),
            )
        for i in range(2):
            RevisionRequest.objects.create(
                project=project, client=cp,
                description=f'Rehearsal revision {i + 1}',
                is_major=True, counts_against_limit=True, status='complete',
            )
        ClientDocument.objects.create(
            client=cp, project=project, direction='to_client',
            file='portal/clients/rehearsal/brand-guide.pdf',
            label='Brand guide',
        )
        SupportTicket.objects.create(
            client=cp, project=project, subject='Add a staff bio',
            description='Please add the new associate to the team page.',
            priority='low', status='resolved',
            resolved_at=self.now - timedelta(days=20),
        )
        contract = Contract.objects.create(
            client=cp, package='essential_build',
            build_price=Decimal('2500.00'), deposit_amount=Decimal('1250.00'),
            timeline_weeks=4, contract_text='Rehearsal contract body.',
            signed=True, signed_at=self.now - timedelta(days=135),
            signed_ip='198.51.100.7', signed_name='Marcus Johnson',
        )
        ContractService.objects.create(
            contract=contract, service_type='build',
            tier_slug='website-essential',
            tier_name='Essential Website Build',
            price=Decimal('2500.00'), deposit_amount=Decimal('1250.00'))
        for kind, amount, sid, delta in (
                ('deposit', '1250.00', 'pi_REHEARSALJOHNSON1', 130),
                ('final', '1250.00', 'pi_REHEARSALJOHNSON2', 100),
                ('maintenance', '299.00', 'in_REHEARSALJOHNSONM1', 30)):
            PaymentRecord.objects.create(
                client=cp, kind=kind, amount=Decimal(amount),
                stripe_id=sid, paid_at=self.now - timedelta(days=delta),
                description=f'Rehearsal {kind} payment',
            )

        # Canonical-side service rows (already Account/Website shaped).
        account = cp.migrated_account
        website = account.websites.first()
        MaintenancePlan.objects.create(
            account=account, website=website,
            tier_slug='maintenance-essentials', status='active',
            stripe_subscription_id='sub_REHEARSALJOHNSONMAINT',
            started_at=self.now - timedelta(days=80),
        )
        Droplet.objects.create(
            account=account, website=website, source='build',
            status='active', do_droplet_id='401234567',
            do_droplet_ip='203.0.113.10', do_size='s-1vcpu-2gb',
            provisioned_at=self.now - timedelta(days=120),
        )
        DomainRegistration.objects.create(
            id=_uid('domain-johnson'), client=cp,
            domain_name='johnsonlawfirm.com', tld='com', status='active',
            registered_at=self.now - timedelta(days=150),
            expires_at=self.now + timedelta(days=215),
            stripe_subscription_id='sub_REHEARSALJOHNSONDOM',
            pricing_tier_slug='domain-standard',
        )
        vault = self._vault(cp, notes='Rehearsal vault.')
        self._ssh_credential(vault, label='Droplet root SSH')

    # 2 ── Moonieful referral ────────────────────────────────────────────
    def _seed_moonieful_client(self):
        """Riverbend Counseling — synced from Moonieful.

        business_type stays blank (never the Law Firm default) and the raw
        intake JSON is the source of truth.
        """
        from clients.models import IntakeResponse, PaymentRecord
        from clients.service_models import MaintenancePlan
        from sync.models import SyncJob

        handoff = self.now - timedelta(days=40)
        cp = self._profile(
            handle='riverbend', firm='Riverbend Counseling',
            person=('Dana', 'Reyes'),
            contact_name='Dana Reyes', phone='404-555-0198',
            website='https://riverbendcounseling.com',
            city='Savannah', state='GA', zip_code='31401',
            business_type='',
            package='moonieful_referred',
            moonieful_client_id=_uid('moonieful-riverbend'),
            synced_from_moonieful=True,
            last_synced_at=self.now - timedelta(days=2),
            moonieful_package='Brand Clarity + Website',
            moonieful_handoff_at=handoff,
            handoff_followup_sent={'day3': (handoff + timedelta(days=3))
                                   .isoformat()},
            stripe_customer_id='cus_REHEARSALRIVERBEND',
            stripe_subscription_id='sub_REHEARSALRIVERBENDMAINT',
            maintenance_active=True,
            maintenance_started_at=self.now - timedelta(days=25),
            stage='live', payment_status='fully_paid',
            launch_date=handoff.date(),
        )
        project = self._project(
            cp, stage='live', live_url='https://riverbendcounseling.com',
            payment_status='fully_paid', moonieful_referred=True,
            moonieful_handoff_at=handoff,
            moonieful_stage_history=[
                {'stage': 'brand_complete', 'at': handoff.isoformat()}],
        )
        IntakeResponse.objects.create(
            project=project, client=cp, completed=True,
            completed_at=handoff,
            moonieful_intake_raw={
                'brand_voice': 'calm, plainspoken',
                'palette': ['#2F5D50', '#E8DCC8'],
                'services': ['individual therapy', 'couples counseling'],
                'source': 'moonieful',
            },
        )
        PaymentRecord.objects.create(
            client=cp, kind='maintenance', amount=Decimal('299.00'),
            stripe_id='in_REHEARSALRIVERBEND1',
            paid_at=self.now - timedelta(days=25),
        )
        account = cp.migrated_account
        MaintenancePlan.objects.create(
            account=account, website=account.websites.first(),
            tier_slug='maintenance-essentials', status='active',
            stripe_subscription_id='sub_REHEARSALRIVERBENDMAINT',
            started_at=self.now - timedelta(days=25),
        )
        SyncJob.objects.create(
            target='moonieful', client=cp,
            event_type='maintenance_activated',
            payload={'aspired_client_id': str(cp.pk)},
            payload_snapshot={'aspired_client_id': str(cp.pk)},
            status='sent', sent_at=self.now - timedelta(days=25),
        )

    # 3 ── Multi-website account ────────────────────────────────────────
    def _seed_multi_website_account(self):
        """Vance Holdings — one payer, two brands, two builds.

        The second Website is created the new way (directly under the
        Account).  Client-level historical rows therefore cannot be split
        by "oldest website wins" and need an explicit mapping.
        """
        from clients.account_models import Website
        from clients.models import (
            ClientDocument, PaymentRecord, SupportTicket)
        from clients.service_models import Droplet, MaintenancePlan

        cp = self._profile(
            handle='vance', firm='Vance Family Law',
            person=('Elena', 'Vance'),
            contact_name='Elena Vance', phone='512-555-0110',
            website='https://vancefamilylaw.com',
            address='900 Congress Ave', city='Austin', state='TX',
            zip_code='78701', business_type='Law Firm',
            package='premium_build',
            stripe_customer_id='cus_REHEARSALVANCE',
            stage='live', payment_status='fully_paid',
            do_droplet_id='401234568', do_droplet_ip='203.0.113.20',
            launch_date=(self.now - timedelta(days=200)).date(),
        )
        first_project = self._project(
            cp, stage='live', package='premium_build',
            live_url='https://vancefamilylaw.com',
            payment_status='fully_paid')
        second_project = self._project(
            cp, stage='review', package='essential_build',
            staging_url='https://staging.vancemediation.com',
            payment_status='deposit_paid')
        # A third, abandoned build — never got its own Website. Used below
        # to model a hand-repair that attached it to the wrong Account.
        third_project = self._project(
            cp, stage='structure', package='essential_build',
            payment_status='awaiting_deposit')
        self.vance_cp = cp
        self.vance_second_project = second_project
        self.vance_third_project = third_project

        account = cp.migrated_account
        first_site = account.websites.first()
        if first_site is not None:
            Website.objects.filter(pk=first_site.pk).update(
                legacy_project=first_project,
                url='https://vancefamilylaw.com',
                stage='live', package='premium_build',
                payment_status='fully_paid',
                created_at=self.now - timedelta(days=210),
            )
            first_site.refresh_from_db()

        second_site = Website.objects.create(
            account=account, name='Vance Mediation Services',
            business_type='Mediation',
            staging_url='https://staging.vancemediation.com',
            stage='review', package='essential_build',
            payment_status='deposit_paid',
            legacy_project=second_project,
            created_at=self.now - timedelta(days=45),
        )
        self.vance_first_site = first_site
        self.vance_second_site = second_site

        # Historical client-level rows — genuinely ambiguous.  The mediation
        # ticket and document belong to the SECOND site, which the generic
        # "oldest website" rule would get wrong.
        SupportTicket.objects.create(
            client=cp, project=second_project,
            subject='Mediation site: intake form not emailing',
            description='Submissions on the mediation staging site vanish.',
            priority='high', status='open')
        ClientDocument.objects.create(
            client=cp, project=second_project, direction='from_client',
            file='portal/clients/rehearsal/mediation-copy.docx',
            label='Mediation page copy')
        ClientDocument.objects.create(
            client=cp, project=first_project, direction='to_client',
            file='portal/clients/rehearsal/family-law-sitemap.pdf',
            label='Family law sitemap')
        PaymentRecord.objects.create(
            client=cp, kind='deposit', amount=Decimal('2250.00'),
            stripe_id='pi_REHEARSALVANCE1',
            paid_at=self.now - timedelta(days=44))

        MaintenancePlan.objects.create(
            account=account, website=first_site,
            tier_slug='maintenance-growth', status='active',
            stripe_subscription_id='sub_REHEARSALVANCEMAINT',
            started_at=self.now - timedelta(days=190))
        Droplet.objects.create(
            account=account, website=first_site, source='build',
            status='active', do_droplet_id='401234568',
            do_droplet_ip='203.0.113.20',
            provisioned_at=self.now - timedelta(days=210))

    # 4 ── Pre-signal orphan ─────────────────────────────────────────────
    def _seed_pre_signal_orphan(self):
        """Delgado Injury — created before the autocreate signal shipped.

        No Account, and its Project has no Website.  This is the shape the
        audit calls client-profile-missing-account.
        """
        from clients.models import PaymentRecord, ProjectStageLog


        cp = self._profile(
            handle='delgado', firm='Delgado Injury Law',
            person=('Rosa', 'Delgado'),
            skip_autocreate=True,
            contact_name='Rosa Delgado', phone='713-555-0123',
            website='https://delgadoinjury.com',
            city='Houston', state='TX', zip_code='77002',
            business_type='Law Firm', package='essential_build',
            stripe_customer_id='cus_REHEARSALDELGADO',
            stage='content', payment_status='deposit_paid',
            deposit_paid_at=self.now - timedelta(days=18),
        )
        project = self._project(
            cp, stage='content', package='essential_build',
            payment_status='deposit_paid',
            deposit_paid_at=self.now - timedelta(days=18))
        ProjectStageLog.objects.create(
            project=project, client=cp, from_stage='design',
            to_stage='content', set_by='admin')
        PaymentRecord.objects.create(
            client=cp, kind='deposit', amount=Decimal('1250.00'),
            stripe_id='pi_REHEARSALDELGADO1',
            paid_at=self.now - timedelta(days=18))
        vault = self._vault(cp)
        self._ssh_credential(vault, label='Staging SSH')

    # 5 ── Account/user mismatch ─────────────────────────────────────────
    def _seed_user_mismatch(self):
        """Pinehurst Dental — the login was rebuilt and the Account kept
        pointing at the retired user row."""
        from clients.account_models import Account

        cp = self._profile(
            handle='pinehurst', firm='Pinehurst Dental',
            person=('Alan', 'Whitfield'),
            contact_name='Alan Whitfield', phone='912-555-0177',
            website='https://pinehurstdental.com',
            city='Macon', state='GA', zip_code='31201',
            business_type='Dental Practice', package='essential_build',
            stripe_customer_id='cus_REHEARSALPINEHURST',
            stage='pre_launch', payment_status='deposit_paid')
        self._project(cp, stage='pre_launch', package='essential_build',
                      payment_status='deposit_paid')

        retired = self._user('pinehurst-old', 'Alan', 'Whitfield')
        Account.objects.filter(legacy_client_profile=cp).update(user=retired)
        self.pinehurst_cp = cp

    # 6 ── Duplicate external identifiers ────────────────────────────────
    def _seed_duplicate_identifiers(self):
        """Two accounts that were merged in Stripe but not here, plus a
        droplet row duplicated by a re-run of the provisioning task."""
        from clients.account_models import Account
        from clients.service_models import Droplet, MaintenancePlan

        shared_customer = 'cus_REHEARSALSHARED'
        primary = self._profile(
            handle='oakridge', firm='Oakridge Wealth Advisors',
            person=('Priya', 'Nandakumar'),
            contact_name='Priya Nandakumar', phone='469-555-0155',
            website='https://oakridgewealth.com',
            city='Plano', state='TX', zip_code='75024',
            business_type='Financial Advisor', package='essential_build',
            stripe_customer_id=shared_customer,
            stage='live', payment_status='fully_paid',
            maintenance_active=True)
        self._project(primary, stage='live', package='essential_build',
                      payment_status='fully_paid')

        secondary = self._profile(
            handle='oakridge-tax', firm='Oakridge Tax Group',
            person=('Priya', 'Nandakumar'),
            contact_name='Priya Nandakumar', phone='469-555-0155',
            city='Plano', state='TX', zip_code='75024',
            business_type='Accounting', package='essential_build',
            stripe_customer_id=shared_customer,
            stage='design', payment_status='deposit_paid')
        self._project(secondary, stage='design', package='essential_build',
                      payment_status='deposit_paid')
        self.oakridge_primary_cp = primary
        self.oakridge_secondary_cp = secondary

        primary_account = primary.migrated_account
        secondary_account = secondary.migrated_account
        primary_site = primary_account.websites.first()
        secondary_site = secondary_account.websites.first()

        # Same maintenance subscription recorded against both accounts.
        for account, site in ((primary_account, primary_site),
                              (secondary_account, secondary_site)):
            MaintenancePlan.objects.create(
                account=account, website=site,
                tier_slug='maintenance-essentials', status='active',
                stripe_subscription_id='sub_REHEARSALDUPEMAINT',
                started_at=self.now - timedelta(days=60))

        # Provisioning task ran twice — one droplet, two rows.
        for account, site in ((primary_account, primary_site),
                              (primary_account, primary_site)):
            Droplet.objects.create(
                account=account, website=site, source='build',
                status='active', do_droplet_id='401234599',
                do_droplet_ip='203.0.113.30',
                provisioned_at=self.now - timedelta(days=70))

        # Same hosting subscription id on two Websites.
        from clients.account_models import Website
        Website.objects.filter(
            pk__in=[s.pk for s in (primary_site, secondary_site) if s]
        ).update(stripe_hosting_subscription_id='sub_REHEARSALDUPEHOST')
        Account.objects.filter(pk=secondary_account.pk).update(
            internal_notes='Merged into Oakridge Wealth in Stripe.')

    # 7 ── Subscription-only account (no build) ──────────────────────────
    def _seed_subscription_only(self):
        """Harbor CPA — bought maintenance for a site we never built."""
        from clients.service_models import MaintenancePlan, SocialMediaPlan

        cp = self._profile(
            handle='harborcpa', firm='Harbor CPA',
            person=('Grant', 'Mueller'),
            skip_website=True,
            contact_name='Grant Mueller', phone='210-555-0188',
            city='San Antonio', state='TX', zip_code='78230',
            business_type='Accounting',
            stripe_customer_id='cus_REHEARSALHARBOR',
            stripe_subscription_id='sub_REHEARSALHARBORMAINT',
            maintenance_active=True,
            maintenance_started_at=self.now - timedelta(days=15))
        account = cp.migrated_account
        MaintenancePlan.objects.create(
            account=account, website=None,
            external_site_url='https://harborcpa.com',
            tier_slug='maintenance-growth', status='active',
            stripe_subscription_id='sub_REHEARSALHARBORMAINT',
            started_at=self.now - timedelta(days=15))
        SocialMediaPlan.objects.create(
            account=account, website=None,
            external_site_url='https://harborcpa.com',
            tier_slug='social-basic', status='active',
            stripe_subscription_id='sub_REHEARSALHARBORSOCIAL',
            started_at=self.now - timedelta(days=10), max_channels=2)

    # 8 ── Field drift ───────────────────────────────────────────────────
    def _seed_field_drift(self):
        """A writer used queryset.update(), so the sync signal never fired
        and Account/Website now disagree with the legacy row."""
        from clients.models import ClientProfile

        ClientProfile.objects.filter(pk=self.johnson_cp.pk).update(
            phone='210-555-0999',
            contact_name='Marcus A. Johnson',
            internal_notes='Renewal call scheduled for next month.',
            city='New Braunfels',
            revision_count=3,
            staging_url='https://staging2.johnsonlawfirm.com',
        )
        ClientProfile.objects.filter(pk=self.vance_cp.pk).update(
            phone='512-555-0111',
            preferred_contact_method='text',
        )

    # 9 ── Website pointing at another account's Project ─────────────────
    def _seed_cross_account_project(self):
        """A hand-repaired Website row that got attached to the wrong
        account's legacy Project during an earlier manual fix."""
        from clients.account_models import Website

        cp = self._profile(
            handle='sable', firm='Sable Creek Realty',
            person=('Tomas', 'Ruiz'),
            contact_name='Tomas Ruiz', phone='210-555-0166',
            city='Boerne', state='TX', zip_code='78006',
            business_type='Real Estate', package='essential_build',
            stripe_customer_id='cus_REHEARSALSABLE',
            stage='design', payment_status='deposit_paid')
        self._project(cp, stage='design', package='essential_build',
                      payment_status='deposit_paid')
        account = cp.migrated_account
        site = account.websites.first()
        if site is not None:
            # Points at a Vance project — wrong account entirely.
            Website.objects.filter(pk=site.pk).update(
                legacy_project=self.vance_third_project)
        self.sable_cp = cp

    # ── Report ──────────────────────────────────────────────────────────

    def _summary(self):
        from clients.account_models import Account, Website
        from clients.models import ClientProfile, Project

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Rehearsal dataset seeded.'))
        for label, model in (
                ('ClientProfile', ClientProfile), ('Project', Project),
                ('Account', Account), ('Website', Website)):
            self.stdout.write(f'  {label}: {model.objects.count()}')
        self.stdout.write(
            f'  database: {settings.DATABASES["default"]["NAME"]}')
