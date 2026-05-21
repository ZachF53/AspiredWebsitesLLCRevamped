"""
Seed realistic test data so the admin dashboard isn't empty in development.

Usage:
    python manage.py seed_test_leads          # add seed leads (idempotent)
    python manage.py seed_test_leads --wipe   # remove seed data and exit (no re-seed)

Seed records are tagged with 'seed' in the `tags` field so they can be
identified and cleaned up without touching real leads.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from outreach.models import (
    EmailReply,
    EmailSent,
    Lead,
    LeadNote,
    OutreachSettings,
)
from outreach.scoring import score_lead


SEED_TAG = 'seed'


# Realistic mix — 5 law firms (the primary target) + 5 small businesses,
# across all stages of the pipeline, with varied scoring signals.
LEADS = [
    # ─── Law firms ───
    {
        'firm_name': 'Whitmore & Garcia LLP',
        'attorney_name': 'Daniela Whitmore',
        'practice_area': 'Family Law',
        'business_type': 'Law Firm',
        'email': 'dwhitmore@whitmoregarcia.example',
        'phone': '(512) 555-0142',
        'website': '',  # no website — hot signal
        'city': 'Austin', 'state': 'Texas',
        'google_rating': 4.6, 'google_review_count': 38,
        'has_google_business': True,
        'status': 'new', 'source': 'state_bar',
    },
    {
        'firm_name': 'Pearson & Sons Personal Injury',
        'attorney_name': 'Marcus Pearson',
        'practice_area': 'Personal Injury',
        'business_type': 'Law Firm',
        'email': 'mpearson@pearsonsons.example',
        'phone': '(713) 555-0184',
        'website': 'https://pearsonsons.example',
        'website_performance_score': 32,
        'website_seo_score': 78,
        'website_mobile_score': 28,
        'audit_run_offset_days': 3,
        'city': 'Houston', 'state': 'Texas',
        'google_rating': 4.2, 'google_review_count': 12,
        'has_google_business': True,
        'status': 'contacted', 'source': 'google_maps',
        'last_contacted_offset_days': 5,
    },
    {
        'firm_name': 'Henderson Estate Planning',
        'attorney_name': 'Priya Henderson',
        'practice_area': 'Estate Planning',
        'business_type': 'Law Firm',
        'email': 'priya@hendersonestate.example',
        'phone': '(404) 555-0211',
        'website': '',  # no website
        'city': 'Atlanta', 'state': 'Georgia',
        'google_rating': None, 'google_review_count': 0,
        'has_google_business': False,
        'status': 'new', 'source': 'state_bar',
    },
    {
        'firm_name': 'Bryant Criminal Defense',
        'attorney_name': 'Wade Bryant',
        'practice_area': 'Criminal Defense',
        'business_type': 'Law Firm',
        'email': 'wade@bryantcd.example',
        'phone': '(210) 555-0167',
        'website': 'https://bryantcd.example',
        'website_performance_score': 91,
        'website_seo_score': 95,
        'website_mobile_score': 88,
        'audit_run_offset_days': 12,
        'city': 'San Antonio', 'state': 'Texas',
        'google_rating': 4.9, 'google_review_count': 127,
        'has_google_business': True,
        'status': 'lost', 'source': 'google_maps',
        'last_contacted_offset_days': 30,
    },
    {
        'firm_name': 'Kim Immigration Law',
        'attorney_name': 'Soo-Yun Kim',
        'practice_area': 'Immigration',
        'business_type': 'Law Firm',
        'email': 'sykim@kimimmigration.example',
        'phone': '(678) 555-0193',
        'website': 'https://kimimmigration.example',
        'website_performance_score': 58,
        'website_seo_score': 71,
        'website_mobile_score': 54,
        'audit_run_offset_days': 1,
        'city': 'Atlanta', 'state': 'Georgia',
        'google_rating': 4.4, 'google_review_count': 21,
        'has_google_business': True,
        'status': 'replied', 'source': 'manual',
        'last_contacted_offset_days': 7,
    },

    # ─── Small businesses ───
    {
        'firm_name': 'Riverside Plumbing Co',
        'attorney_name': 'Tom Acuna',
        'practice_area': '',
        'business_type': 'Contractor',
        'email': 'tom@riversideplumb.example',
        'phone': '(214) 555-0238',
        'website': 'https://riversideplumb.example',
        'website_performance_score': 41,
        'city': 'Dallas', 'state': 'Texas',
        'google_rating': 4.7, 'google_review_count': 89,
        'has_google_business': True,
        'status': 'call_booked', 'source': 'google_maps',
        'last_contacted_offset_days': 2,
    },
    {
        'firm_name': 'Tortilla & Toast Café',
        'attorney_name': 'Carmen Vega',
        'practice_area': '',
        'business_type': 'Restaurant',
        'email': '',  # no email captured yet
        'phone': '(210) 555-0299',
        'website': 'https://tortillaandtoast.example',
        'website_performance_score': 68,
        'city': 'San Antonio', 'state': 'Texas',
        'google_rating': 4.8, 'google_review_count': 312,
        'has_google_business': True,
        'status': 'new', 'source': 'manual',
    },
    {
        'firm_name': 'Brookhaven Dental Group',
        'attorney_name': 'Dr. Rachel Brookhaven',
        'practice_area': '',
        'business_type': 'Healthcare',
        'email': 'office@brookhavendental.example',
        'phone': '(404) 555-0354',
        'website': 'https://brookhavendental.example',
        'website_performance_score': 78,
        'city': 'Atlanta', 'state': 'Georgia',
        'google_rating': 4.5, 'google_review_count': 67,
        'has_google_business': True,
        'status': 'proposal_sent', 'source': 'audit_tool',
        'last_contacted_offset_days': 4,
        'inquiry_text': 'Ran the audit on our site and the mobile score was rough. Interested in talking about a rebuild — what would that look like?',
    },
    {
        'firm_name': 'Lone Star Electric',
        'attorney_name': 'Eli Donovan',
        'practice_area': '',
        'business_type': 'Contractor',
        'email': 'eli@lonestarelec.example',
        'phone': '(512) 555-0421',
        'website': '',  # no website
        'city': 'Austin', 'state': 'Texas',
        'google_rating': None, 'google_review_count': 4,
        'has_google_business': False,
        'status': 'won', 'source': 'contact_form',
        'last_contacted_offset_days': 18,
        'inquiry_text': 'My buddy referred me — said you built his roofing site. I need a real website, mine is a free thing I made on Wix years ago. Call me when you can.',
    },
    {
        'firm_name': 'Magnolia Boutique',
        'attorney_name': 'Anya Calderon',
        'practice_area': '',
        'business_type': 'Retail',
        'email': 'anya@magnoliaboutique.example',
        'phone': '(706) 555-0468',
        'website': 'https://magnoliaboutique.example',
        'website_performance_score': 22,
        'website_seo_score': 45,
        'website_mobile_score': 19,
        'audit_run_offset_days': 6,
        'city': 'Atlanta', 'state': 'Georgia',
        'google_rating': 4.3, 'google_review_count': 19,
        'has_google_business': True,
        'status': 'archived', 'source': 'cold_email' if False else 'google_maps',
    },
]


# A handful of notes, sent emails, and replies attached to specific leads
NOTES = [
    ('Pearson & Sons Personal Injury', 'Spoke briefly — said they’d had a bad experience with FindLaw. Sending follow-up tomorrow.'),
    ('Brookhaven Dental Group', 'Proposal sent. They asked for a custom intake form integration — billable add-on if they want it.'),
    ('Lone Star Electric', 'Closed. Deposit received. Build kickoff Monday.'),
]

EMAILS_SENT = [
    ('Pearson & Sons Personal Injury', 'Quick thought on your site', 5, 1, False, False),
    ('Kim Immigration Law',            'Following up — site rebuild?', 7, 1, True, True),
    ('Brookhaven Dental Group',        'Your audit results + next step', 4, 1, True, False),
    ('Lone Star Electric',             'Confirmation + onboarding link', 18, 2, True, True),
]

# Reply with needs_human=True so the Needs You badge appears (=1)
REPLIES = [
    {
        'firm_name': 'Kim Immigration Law',
        'classification': 'question',
        'subject': 'RE: Following up — site rebuild?',
        'body': 'Thanks for reaching out. We are interested but my partner has concerns about our current host. Can you do a 15-min call this Thursday afternoon to talk through migration? Also — do you handle multi-lingual sites? Most of our intake is Spanish-language.',
        'needs_human': True,
    },
]


class Command(BaseCommand):
    help = 'Seed realistic test data so the admin dashboard renders with content.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--wipe',
            action='store_true',
            help='Delete seed data (leads tagged "seed") and exit — does NOT re-seed.',
        )

    def handle(self, *args, **options):
        if options['wipe']:
            wiped_leads, _ = Lead.objects.filter(tags__icontains=SEED_TAG).delete()
            self.stdout.write(self.style.WARNING(
                f'Wiped {wiped_leads} seed lead(s) and their cascaded notes/emails/replies. '
                'Run "seed_test_leads" (no flag) to re-seed.'
            ))
            return  # --wipe means wipe and stop — do not re-seed.

        now = timezone.now()
        created = 0
        skipped = 0

        for data in LEADS:
            firm_name = data['firm_name']
            if Lead.objects.filter(firm_name=firm_name).exists():
                skipped += 1
                continue

            audit_offset = data.pop('audit_run_offset_days', None)
            last_contacted_offset = data.pop('last_contacted_offset_days', None)

            lead_kwargs = {**data}
            lead_kwargs.setdefault('inquiry_text', '')
            lead_kwargs['tags'] = SEED_TAG
            if audit_offset is not None:
                lead_kwargs['audit_run_at'] = now - timedelta(days=audit_offset)
            if last_contacted_offset is not None:
                lead_kwargs['last_contacted_at'] = now - timedelta(days=last_contacted_offset)

            # Score from the same signals scrapers feed
            score, temperature = score_lead(lead_kwargs)
            lead_kwargs['score'] = score
            lead_kwargs['temperature'] = temperature

            Lead.objects.create(**lead_kwargs)
            created += 1

        # Notes
        notes_added = 0
        for firm_name, note_text in NOTES:
            lead = Lead.objects.filter(firm_name=firm_name).first()
            if lead and not LeadNote.objects.filter(lead=lead, note=note_text).exists():
                LeadNote.objects.create(lead=lead, note=note_text)
                notes_added += 1

        # Sent emails
        emails_added = 0
        for firm_name, subject, days_ago, step, opened, replied in EMAILS_SENT:
            lead = Lead.objects.filter(firm_name=firm_name).first()
            if lead and not EmailSent.objects.filter(lead=lead, subject=subject).exists():
                EmailSent.objects.create(
                    lead=lead,
                    subject=subject,
                    body='(seed body)',
                    from_email='zacherylong@aspiredwebsites.com',
                    sequence_step=step,
                    opened=opened,
                    opened_at=(now - timedelta(days=days_ago, hours=-2)) if opened else None,
                    replied=replied,
                    replied_at=(now - timedelta(days=days_ago - 1)) if replied else None,
                    sent_at=now - timedelta(days=days_ago),
                )
                emails_added += 1

        # Replies (for the Needs You badge)
        replies_added = 0
        for r in REPLIES:
            lead = Lead.objects.filter(firm_name=r['firm_name']).first()
            if lead and not EmailReply.objects.filter(lead=lead, subject=r['subject']).exists():
                # Try to link to the matching EmailSent
                email_sent = EmailSent.objects.filter(
                    lead=lead, subject__icontains=r['subject'].replace('RE: ', '')
                ).first()
                EmailReply.objects.create(
                    lead=lead,
                    email_sent=email_sent,
                    classification=r['classification'],
                    subject=r['subject'],
                    body=r['body'],
                    needs_human=r['needs_human'],
                )
                replies_added += 1

        # Make sure the OutreachSettings singleton exists with sensible defaults
        OutreachSettings.load()

        self.stdout.write(self.style.SUCCESS(
            f'Seed complete — leads: +{created} (skipped {skipped}), '
            f'notes: +{notes_added}, emails: +{emails_added}, replies: +{replies_added}'
        ))
