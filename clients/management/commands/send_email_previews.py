"""
Fire every branded client-facing email to a single recipient (default
zacherylong's test inbox) for design review. ZERO side effects on real
client state — every send is constructed from synthetic context.

Usage:
    python manage.py send_email_previews
    python manage.py send_email_previews --to someone@example.com
"""

import datetime
import io

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Send one of every branded transactional email to a recipient '
        'for design review (default: football45353@gmail.com).')

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            default='football45353@gmail.com',
            help='Recipient email address (default: football45353@gmail.com)')

    def handle(self, *args, **opts):
        to = opts['to']
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Sending email previews to {to}\n'))

        sent = []

        # All previews use `send_branded` directly (NOT the real send
        # functions) so they don't mutate token state, scan rows, etc.
        # The recipient is overridden to `to` for every preview.
        from clients.emails import send_branded

        # 1. Onboarding setup
        self._fire(
            'send_branded',
            send_branded,
            subject='[PREVIEW 1/18] Your Aspired Websites account is ready',
            template='onboarding_setup',
            context={
                'first_name': 'Sample',
                'setup_url': (
                    'https://aspiredwebsites.com/onboarding/setup/'
                    '00000000-0000-0000-0000-000000000000/'),
                'preheader': (
                    'Set up your password and PIN to access your portal.'),
            },
            recipient_list=[to],
            text_body=(
                'Welcome aboard, Sample.\n\n'
                'Thank you for your payment — your Aspired Websites '
                'account is ready to be set up.\n\n'
                'https://aspiredwebsites.com/onboarding/setup/...\n\n'
                '— Zachery Long\nAspired Websites LLC\n'),
            secure=True,
            label='1. onboarding_setup', sent=sent,
        )

        # 2. Account setup complete
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 2/18] Your account is ready — one more step'),
            template='account_setup_complete',
            context={
                'first_name': 'Sample',
                'intake_url': 'https://aspiredwebsites.com/portal/intake/',
                'preheader': (
                    'Submit your intake form and we\'ll start building.'),
            },
            recipient_list=[to],
            text_body=(
                'Hi Sample, your account is set up. Next: complete '
                'your intake form so we can start building.\n\n'
                'https://aspiredwebsites.com/portal/intake/\n\n'
                '— Zachery Long\nAspired Websites LLC\n'),
            secure=True,
            label='2. account_setup_complete', sent=sent,
        )

        # 3. Intake received
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 3/18] We\'ve received your intake'),
            template='intake_received',
            context={
                'first_name': 'Sample',
                'portal_url': 'https://aspiredwebsites.com/portal/',
                'preheader': (
                    'We\'ll reach out within 1 business day.'),
            },
            recipient_list=[to],
            text_body=(
                'Thanks Sample — got your intake. We\'ll reach out '
                'within 1 business day.\n\n'
                '— Zachery Long\nAspired Websites LLC\n'),
            label='3. intake_received', sent=sent,
        )

        # 4. Setup reminder
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 4/18] Reminder: Set up your '
                'Aspired Websites account'),
            template='setup_reminder',
            context={
                'first_name': 'Sample',
                'setup_url': (
                    'https://aspiredwebsites.com/onboarding/setup/'
                    '00000000-0000-0000-0000-000000000000/'),
                'preheader': (
                    'Your account is still waiting to be set up.'),
            },
            recipient_list=[to],
            text_body='Hi Sample — just a nudge to set up your account.',
            secure=True,
            label='4. setup_reminder', sent=sent,
        )

        # 5. Intake reminder
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 5/18] Action needed: Complete your '
                'intake form'),
            template='intake_reminder',
            context={
                'first_name': 'Sample',
                'intake_url': 'https://aspiredwebsites.com/portal/intake/',
                'preheader': (
                    'Submit your intake form so we can start building.'),
            },
            recipient_list=[to],
            text_body='Hi Sample — your intake is still waiting.',
            label='5. intake_reminder', sent=sent,
        )

        # 6. Contract ready
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 6/18] Your contract is ready to sign'),
            template='contract_ready',
            context={
                'name': 'Sample',
                'sign_url': (
                    'https://aspiredwebsites.com/portal/contract/'
                    '00000000-0000-0000-0000-000000000000/'),
                'preheader': 'Review and sign to lock in your project.',
            },
            recipient_list=[to],
            text_body='Hi Sample — your contract is ready to sign.',
            secure=True,
            label='6. contract_ready', sent=sent,
        )

        # 7. Contract signed
        self._fire(
            'send_branded',
            send_branded,
            subject='[PREVIEW 7/18] Your contract is signed',
            template='contract_signed',
            context={
                'name': 'Sample',
                'preheader': 'Your deposit invoice is on its way.',
            },
            recipient_list=[to],
            text_body='Thanks Sample — your contract is signed.',
            label='7. contract_signed', sent=sent,
        )

        # 8. Welcome (deposit cleared)
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 8/18] Welcome to Aspired Websites — '
                'your project is active'),
            template='welcome_deposit',
            context={
                'name': 'Sample',
                'intake_url': (
                    'https://aspiredwebsites.com/portal/intake/'),
                'login_url': 'https://aspiredwebsites.com/login/',
                'preheader': (
                    'Project active — complete your intake to begin.'),
            },
            recipient_list=[to],
            text_body='Welcome aboard, Sample. Your project is active.',
            label='8. welcome_deposit', sent=sent,
        )

        # 9. Intake reminder (contract-flow Day 2/4 — same template)
        self._fire(
            'send_branded',
            send_branded,
            subject='[PREVIEW 9/18] Quick reminder: your intake form',
            template='intake_reminder',
            context={
                'first_name': 'Sample',
                'intake_url': (
                    'https://aspiredwebsites.com/portal/intake/'),
                'preheader': (
                    'Your project is on hold until intake is in.'),
            },
            recipient_list=[to],
            text_body='Hi Sample — quick reminder to finish your intake.',
            label='9. intake_reminder (contract-flow)', sent=sent,
        )

        # 10. Payment failed
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 10/18] Payment issue on your '
                'Aspired Websites account'),
            template='payment_failed',
            context={
                'name': 'Sample',
                'invoices_url': (
                    'https://aspiredwebsites.com/portal/invoices/'),
                'preheader': 'Please update your payment details.',
            },
            recipient_list=[to],
            text_body='Hi Sample — we could not process your payment.',
            label='10. payment_failed', sent=sent,
        )

        # 12. Maintenance handoff
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 12/18] Your site is live — '
                'set up your maintenance plan'),
            template='maintenance_handoff',
            context={
                'name': 'Sample',
                'handoff_url': (
                    'https://aspiredwebsites.com/maintenance/start/'
                    '?token=DEMO'),
                'is_followup': False,
                'subject_line': (
                    'Your site is live — set up your maintenance plan'),
                'preheader': (
                    'Pick a plan to keep your site secure and updated.'),
            },
            recipient_list=[to],
            text_body='Hi Sample — your site is live. Pick a plan.',
            secure=True,
            label='12. maintenance_handoff', sent=sent,
        )

        # 13. Monthly report
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 13/18] Your Monthly Report — May 2026'),
            template='monthly_report',
            context={
                'name': 'Sample',
                'month_str': 'May 2026',
                'uptime': '99.98',
                'portal_url': (
                    'https://aspiredwebsites.com/portal/reports/'),
                'preheader': 'May 2026 performance report attached.',
            },
            recipient_list=[to],
            text_body='Hi Sample — your monthly report for May 2026.',
            attachments=[
                ('demo-monthly-report.txt',
                 b'Demo placeholder PDF content.',
                 'text/plain')],
            label='13. monthly_report (with attachment)', sent=sent,
        )

        # 14. NPS survey
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 14/18] Quick question about your website'),
            template='nps_survey',
            context={
                'name': 'Sample',
                'base_url': (
                    'https://aspiredwebsites.com/nps/'
                    '00000000-0000-0000-0000-000000000000/'),
                'scores_low': list(range(0, 6)),
                'scores_high': list(range(6, 11)),
                'preheader': (
                    'On a scale of 0–10, how likely are you to '
                    'recommend us?'),
            },
            recipient_list=[to],
            text_body=(
                'Hi Sample — on a scale of 0 to 10, how likely are '
                'you to recommend us?'),
            label='14. nps_survey', sent=sent,
        )

        # 15. Testimonial request
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 15/18] Would you share your experience?'),
            template='testimonial_request',
            context={
                'name': 'Sample',
                'preheader': (
                    'A quick favor — a 1–2 minute video of your '
                    'experience.'),
            },
            recipient_list=[to],
            text_body='Hi Sample — would you record a quick video?',
            label='15. testimonial_request', sent=sent,
        )

        # 16/17. Security report (auto-send and manual share the template)
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 16-17/18] Your Security Report — May 2026'),
            template='security_report',
            context={
                'name': 'Sample',
                'client_firm': 'Sample Firm',
                'month_str': 'May 2026',
                'critical_count': 0,
                'high_count': 2,
                'security_url': (
                    'https://aspiredwebsites.com/portal/security/'),
                'preheader': (
                    '2 high severity issues identified.'),
            },
            recipient_list=[to],
            text_body=(
                'Hi Sample — security assessment attached. '
                '2 high severity issues identified.'),
            attachments=[
                ('demo-security-report.txt',
                 b'Demo placeholder PDF content.',
                 'text/plain')],
            label='16+17. security_report (with attachment)', sent=sent,
        )

        # 18. Intelligence suggestion
        # Build a synthetic suggestion-like object the template can read.
        class _Suggestion:
            title = 'Add a chatbot to handle after-hours intake'
            description = (
                'Your site gets 30+ visits after business hours each '
                'week, but contact form submits drop sharply after 6pm. '
                'A simple chatbot could capture these visitors and let '
                'them schedule a callback or submit basic case info '
                'while you sleep.')
            expected_impact = (
                '20-30 additional captured leads/month based on current '
                'traffic patterns.')
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 18/18] Website Improvement Opportunity — '
                'Sample Firm'),
            template='intelligence_suggestion',
            context={
                'name': 'Sample',
                'suggestion': _Suggestion(),
                'investment_line': 'Investment: $450 one-time',
                'plan_para': (
                    'This falls outside your current maintenance plan '
                    'scope, but I can implement it as a one-time add-on.'),
                'approve_url': (
                    'https://aspiredwebsites.com/intelligence/respond/'
                    '00000000-0000-0000-0000-000000000000/approve/'),
                'decline_url': (
                    'https://aspiredwebsites.com/intelligence/respond/'
                    '00000000-0000-0000-0000-000000000000/decline/'),
                'preheader': (
                    'Add a chatbot to handle after-hours intake'),
            },
            recipient_list=[to],
            text_body='Hi Sample — recommendation: add a chatbot.',
            secure=True,
            label='18. intelligence_suggestion', sent=sent,
        )

        # 19. Annual report
        self._fire(
            'send_branded',
            send_branded,
            subject=(
                '[PREVIEW 19/18] Your 2025 Annual Website '
                'Performance Report'),
            template='annual_report',
            context={
                'name': 'Sample',
                'client_firm': 'Sample Firm',
                'report_year': 2025,
                'preheader': (
                    'A full year of performance, security, and growth.'),
            },
            recipient_list=[to],
            text_body=(
                'Hi Sample — your 2025 Annual Business Health Report '
                'is attached.'),
            attachments=[
                ('demo-annual-report.txt',
                 b'Demo placeholder PDF content.',
                 'text/plain')],
            label='19. annual_report (with attachment)', sent=sent,
        )

        # Summary.
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Sent {len(sent)} previews to {to}'))
        for s in sent:
            self.stdout.write(f'  ✓ {s}')

    def _fire(self, _name, fn, *, label, sent, **kwargs):
        """Run one send_branded call, log success/failure."""
        try:
            fn(**kwargs)
            sent.append(label)
            self.stdout.write(self.style.SUCCESS(
                f'  sent: {label}'))
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(
                f'  FAILED: {label} — {exc}'))
