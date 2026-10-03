"""
A staff "view as client" session must be invisible and inert.

Two properties this protects, both of which the obvious implementation
gets wrong:

1. INVISIBLE. The client is never told a session happened, so the portal
   must not write anything while one is active. That is not the default:
   several portal GETs mutate client data today. The worst is
   /portal/intake/, which calls _ensure_project_for_unlocked_intake and
   stamps `payment_status='fully_paid'` + `final_paid_at` on a plain GET
   — so viewing a pending-intake client would mark an unpaid client as
   paid in full. The suppression tests below are the point of this file.

2. INERT. Disabling buttons in the DOM is cosmetic: the operator is
   still authenticated as themselves with a live session and a valid
   CSRF token, so a re-enabled control would submit successfully.
   ImpersonationGuardMiddleware is the real boundary and these tests
   drive it over the wire, not through the JS.

Resolution must also fail closed — a session that stops validating has
to degrade to the operator's own portal, never to a third party's.
"""

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from admin_dashboard.models import ImpersonationSession
from clients.account_models import Account, Website

User = get_user_model()


def _fresh_account(user, name, **kwargs):
    """Account for `user`, reusing the one the autocreate signal makes.

    Every new User gets an Account from clients.signals, so a plain
    create() here raises on the OneToOne. Websites are cleared so each
    test builds the exact shape it needs.
    """
    account = Account.objects.filter(user=user).first()
    if account is None:
        account = Account.objects.create(user=user, name=name, **kwargs)
    else:
        account.name = name
        for field, value in kwargs.items():
            setattr(account, field, value)
        account.save()
    account.websites.all().delete()
    return account


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ImpersonationBaseTests(TestCase):

    def setUp(self):
        self.operator = User.objects.create_user(
            username='op', email='op@example.com', password='pw-123456',
            is_staff=True)
        self.operator_account = _fresh_account(
            self.operator, 'Operator Co', onboarding_status='complete')
        # The operator needs a Website of their own: the fail-closed
        # tests land on their personal portal, and the dashboard reads
        # Website-only fields off whatever _active_project returns — an
        # account with zero websites raises AttributeError in
        # _stage_steps.
        self.operator_site = Website.objects.create(
            account=self.operator_account, name='Operator Site',
            onboarding_status='complete')

        self.client_user = User.objects.create_user(
            username='acme', email='acme@example.com', password='pw-123456')
        self.target = _fresh_account(
            self.client_user, 'Acme Co', onboarding_status='complete')
        self.target_site = Website.objects.create(
            account=self.target, name='Acme Site',
            onboarding_status='complete')

    def start_view_as(self, account=None):
        account = account or self.target
        self.client.force_login(self.operator)
        return self.client.post(
            reverse('admin_dashboard:v2_account_view_as',
                    args=[account.id]))


class AccessTests(ImpersonationBaseTests):

    def test_staff_can_start_a_session_and_it_is_logged(self):
        resp = self.start_view_as()
        self.assertEqual(resp.status_code, 302)

        log = ImpersonationSession.objects.get()
        self.assertEqual(log.operator, self.operator)
        self.assertEqual(log.account, self.target)
        self.assertIsNone(log.ended_at)
        self.assertEqual(log.blocked_attempts, 0)

    def test_a_non_staff_user_cannot_start_a_session(self):
        self.client.force_login(self.client_user)
        resp = self.client.post(
            reverse('admin_dashboard:v2_account_view_as',
                    args=[self.target.id]))
        self.assertNotEqual(resp.status_code, 200)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_starting_a_session_is_post_only(self):
        """As a GET this would fire from any prefetch of the URL."""
        self.client.force_login(self.operator)
        resp = self.client.get(
            reverse('admin_dashboard:v2_account_view_as',
                    args=[self.target.id]))
        self.assertEqual(resp.status_code, 405)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_the_operator_is_not_logged_in_as_the_client(self):
        """login() would cycle the session key and stamp the client's
        last_login — the fingerprint this feature must not leave."""
        self.client_user.refresh_from_db()
        self.assertIsNone(self.client_user.last_login)

        self.start_view_as()
        self.client.get(reverse('clients:dashboard'))

        self.client_user.refresh_from_db()
        self.assertIsNone(self.client_user.last_login)

    def test_resolution_fails_closed_on_operator_mismatch(self):
        self.start_view_as()
        session = self.client.session
        session['impersonate_operator_id'] = self.client_user.pk
        session.save()

        resp = self.client.get(reverse('clients:dashboard'))
        self.assertEqual(resp.status_code, 200)
        # Degraded to the operator's own portal, not the target's.
        # Asserted on the resolved context rather than the HTML: the
        # "now viewing X" flash message from begin() is still queued at
        # this point and would match a body search for the target name.
        self.assertEqual(resp.context['account'], self.operator_account)
        self.assertFalse(resp.context['impersonating'])
        self.assertNotIn('impersonate_account_id', self.client.session)

        log = ImpersonationSession.objects.get()
        self.assertEqual(log.end_reason, 'operator_mismatch')
        self.assertIsNotNone(log.ended_at)

    def test_losing_staff_status_mid_session_fails_closed(self):
        self.start_view_as()
        self.operator.is_staff = False
        self.operator.save(update_fields=['is_staff'])

        resp = self.client.get(reverse('clients:dashboard'))
        self.assertEqual(resp.context['account'], self.operator_account)
        self.assertNotIn('impersonate_account_id', self.client.session)


class FidelityTests(ImpersonationBaseTests):

    def test_the_portal_renders_the_target_not_the_operator(self):
        self.start_view_as()
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['account'], self.target)
        self.assertEqual(resp.context['website'], self.target_site)
        self.assertTrue(resp.context['impersonating'])

    def test_the_banner_names_the_account_being_viewed(self):
        self.start_view_as()
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertContains(resp, 'portal-impersonation-bar')
        self.assertContains(resp, 'Exit view-as')

    def test_the_button_disabling_script_is_loaded(self):
        """Without this tag the portal renders with live buttons. The
        middleware still refuses the submits, but the operator would be
        clicking controls that look functional."""
        self.start_view_as()
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertContains(resp, 'portal_readonly.js')

    def test_exit_form_itself_carries_the_allow_attribute(self):
        """portal_readonly.js's capture-phase submit listener checks
        evt.target (the <form>), not the button inside it — closest()
        only looks at an element and its ancestors, never descendants.
        An allow attribute on the button alone does not exempt the form,
        so the Exit button looked clickable but silently did nothing
        (the listener ate every submit). Regression test for that exact
        bug: the <form> tag must carry data-readonly-allow too."""
        self.start_view_as()
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertContains(
            resp,
            '<form method="post" action="/portal/exit-view-as/" data-readonly-allow>')

    def test_no_banner_or_script_outside_a_session(self):
        self.client.force_login(self.client_user)
        resp = self.client.get(reverse('clients:dashboard'))
        self.assertNotContains(resp, 'portal-impersonation-bar')
        self.assertNotContains(resp, 'portal_readonly.js')

    def test_a_pending_intake_client_shows_the_intake_gate(self):
        """The headline use case — see the locked-down portal a client
        who has not completed intake actually gets."""
        self.target_site.onboarding_status = 'pending_intake'
        self.target_site.save(update_fields=['onboarding_status'])

        self.start_view_as()
        resp = self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            reverse('clients:intake'),
            [url for url, _code in resp.redirect_chain])


class SilenceTests(ImpersonationBaseTests):
    """Nothing may be written to the client's data by a view-as GET."""

    def test_viewing_intake_does_not_mark_the_client_as_paid(self):
        """The single most damaging write in the portal. On a plain GET,
        _ensure_project_for_unlocked_intake sets payment_status to
        'fully_paid' and stamps final_paid_at."""
        self.target_site.onboarding_status = 'pending_intake'
        self.target_site.payment_status = 'awaiting_deposit'
        self.target_site.stage = ''
        self.target_site.save()

        self.start_view_as()
        resp = self.client.get(reverse('clients:intake'))
        self.assertEqual(resp.status_code, 200)

        self.target_site.refresh_from_db()
        self.assertEqual(self.target_site.payment_status, 'awaiting_deposit')
        self.assertIsNone(self.target_site.final_paid_at)
        self.assertEqual(self.target_site.stage, '')

    def test_viewing_intake_creates_no_intake_row(self):
        from clients.models import IntakeResponse

        self.target_site.onboarding_status = 'pending_intake'
        self.target_site.save(update_fields=['onboarding_status'])

        self.start_view_as()
        self.client.get(reverse('clients:intake'))

        self.assertFalse(
            IntakeResponse.objects.filter(website_new=self.target_site)
            .exists())

    def test_viewing_the_referral_page_mints_no_referral_code(self):
        from clients.models import ReferralLink

        self.start_view_as()
        resp = self.client.get(reverse('clients:portal_referral'))
        self.assertEqual(resp.status_code, 200)

        self.assertFalse(
            ReferralLink.objects.filter(account_new=self.target).exists())

    def test_a_real_client_still_gets_their_referral_code(self):
        """Guard against the suppression leaking into normal traffic."""
        from clients.models import ReferralLink

        self.client.force_login(self.client_user)
        self.client.get(reverse('clients:portal_referral'))

        self.assertTrue(
            ReferralLink.objects.filter(account_new=self.target).exists())

    def test_a_real_client_still_gets_their_intake_row(self):
        from clients.models import IntakeResponse

        self.target_site.onboarding_status = 'pending_intake'
        self.target_site.save(update_fields=['onboarding_status'])

        self.client.force_login(self.client_user)
        self.client.get(reverse('clients:intake'))

        self.assertTrue(
            IntakeResponse.objects.filter(website_new=self.target_site)
            .exists())

    def test_walking_the_portal_sends_no_email(self):
        self.start_view_as()
        for name in ('clients:dashboard', 'clients:files',
                     'clients:revisions', 'clients:support',
                     'clients:portal_referral', 'clients:settings'):
            self.client.get(reverse(name))
        self.assertEqual(mail.outbox, [])


class BlockingTests(ImpersonationBaseTests):

    # Action routes that take no URL args, so the whole set can be
    # driven in one loop. Each is a real state change for a client.
    POST_ROUTES = (
        'clients:intake_save',
        'clients:revision_new',
        'clients:support_new',
        'clients:file_upload',
        'clients:settings',
    )

    def test_every_action_post_is_refused(self):
        self.start_view_as()
        for name in self.POST_ROUTES:
            with self.subTest(route=name):
                resp = self.client.post(reverse(name), {})
                self.assertEqual(resp.status_code, 403)

    def test_blocked_attempts_are_counted_on_the_audit_row(self):
        self.start_view_as()
        self.client.post(reverse('clients:support_new'), {})
        self.client.post(reverse('clients:revision_new'), {})

        log = ImpersonationSession.objects.get()
        self.assertEqual(log.blocked_attempts, 2)

    def test_the_credentials_page_is_blocked_on_get(self):
        """It renders stored site passwords in plaintext."""
        self.start_view_as()
        resp = self.client.get(reverse('clients:credentials'))
        self.assertEqual(resp.status_code, 403)

    def test_the_credentials_pin_cannot_be_fumbled_for_the_client(self):
        """A wrong PIN writes failed-attempt and lockout columns on the
        Account, which would lock the real client out of their own
        credentials."""
        before = self.target.client_pin_failed_attempts

        self.start_view_as()
        self.client.post(reverse('clients:credentials'), {'pin': '0000'})

        self.target.refresh_from_db()
        self.assertEqual(self.target.client_pin_failed_attempts, before)

    def test_a_blocked_htmx_request_gets_a_body_to_render(self):
        """A bare 403 would swap an empty region into the page."""
        self.start_view_as()
        resp = self.client.post(
            reverse('clients:intake_save'), {}, HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b'portal-readonly-block', resp.content)

    def test_a_real_client_can_still_post(self):
        """The guard must be inert outside a view-as session."""
        self.client.force_login(self.client_user)
        resp = self.client.post(reverse('clients:support_new'), {})
        self.assertNotEqual(resp.status_code, 403)


class LifecycleTests(ImpersonationBaseTests):

    def test_exiting_closes_the_audit_row(self):
        self.start_view_as()
        resp = self.client.post(reverse('clients:exit_view_as'))
        self.assertEqual(resp.status_code, 302)

        log = ImpersonationSession.objects.get()
        self.assertIsNotNone(log.ended_at)
        self.assertEqual(log.end_reason, 'manual_exit')
        self.assertNotIn('impersonate_account_id', self.client.session)

    def test_the_exit_route_survives_the_block(self):
        """Exiting is a POST under /portal/ — if the guard ate it the
        operator would be trapped until the cookie expired."""
        self.start_view_as()
        self.client.post(reverse('clients:exit_view_as'))
        self.assertNotIn('impersonate_account_id', self.client.session)

    def test_logging_out_closes_the_audit_row(self):
        """logout() flushes the session, so without the signal receiver
        the row would stay open forever with no ended_at."""
        self.start_view_as()
        self.client.post(reverse('public:logout'))

        log = ImpersonationSession.objects.get()
        self.assertIsNotNone(log.ended_at)
        self.assertEqual(log.end_reason, 'logout')

    def test_starting_a_second_session_closes_the_first(self):
        other_user = User.objects.create_user(
            username='beta', email='beta@example.com', password='pw-123456')
        other = _fresh_account(other_user, 'Beta Co',
                               onboarding_status='complete')
        Website.objects.create(account=other, name='Beta Site',
                               onboarding_status='complete')

        self.start_view_as()
        self.start_view_as(other)

        closed = ImpersonationSession.objects.get(account=self.target)
        self.assertEqual(closed.end_reason, 'superseded')
        self.assertIsNotNone(closed.ended_at)

        live = ImpersonationSession.objects.get(account=other)
        self.assertIsNone(live.ended_at)

    def test_the_operators_own_website_choice_is_not_clobbered(self):
        own_site = self.operator_site

        self.client.force_login(self.operator)
        self.client.get(reverse('clients:dashboard'))
        self.assertEqual(
            self.client.session.get('active_website_slug'), own_site.slug)

        self.start_view_as()
        self.client.get(reverse('clients:dashboard'))
        self.client.post(reverse('clients:exit_view_as'))

        # The impersonated site must not be left behind as the
        # operator's active website.
        self.assertNotEqual(
            self.client.session.get('active_website_slug'),
            self.target_site.slug)
