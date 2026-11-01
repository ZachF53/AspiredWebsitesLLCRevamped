"""
Trial lifecycle webhooks: idempotency, and the right GHL tag per event.

The GHL client is mocked at the outreach.ghl boundary, so these tests
assert the exact calls that would go over the wire. That matters more
than usual here because two of GHL's behaviours are destructive and
invisible in a passing test that mocks too high up:

  * a `tags` key on /contacts/upsert REPLACES the contact's whole tag
    array, wiping client status and re-firing intake automation;
  * tags are what fire workflows, so a duplicated tag is a duplicated
    customer email, not a no-op.

So there is a test asserting no upsert anywhere ever carries `tags`,
and the idempotency tests count calls rather than checking end state.
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.pricing_models import ServiceTier
from billing.terms_models import TermsAcceptance
from billing.webhook_models import StripeWebhookEvent

User = get_user_model()

VOICE_SLUG = 'hvac-voice-receptionist'
REVIEW_SLUG = 'hvac-review-system'


def _tier(slug=VOICE_SLUG, name='AI Voice Receptionist'):
    tier, _ = ServiceTier.objects.update_or_create(
        slug=slug, defaults=dict(
            category='addon', name=name, price=Decimal('97.00'),
            is_recurring=True, billing_interval='month', is_active=True,
            stripe_price_id=f'price_{slug}'))
    return tier


def _acceptance(*, slug=VOICE_SLUG, sub='sub_test123', email='b@example.com',
                converted_at=None):
    return TermsAcceptance.objects.create(
        email=email, product_slug=slug, terms_version='2026-11-01',
        accepted_at=timezone.now(), stripe_customer_id='cus_test123',
        stripe_subscription_id=sub, converted_at=converted_at)


def _event(event_type, obj, *, event_id='evt_test1'):
    return {'id': event_id, 'type': event_type, 'data': {'object': obj}}


def _trialing_sub(*, slug=VOICE_SLUG, sub_id='sub_test123'):
    return {
        'id': sub_id, 'customer': 'cus_test123', 'status': 'trialing',
        'trial_end': 1794000000,
        'metadata': {'trial_signup': '1', 'product_slug': slug,
                     'tier_slug': slug, 'product_type': 'maintenance',
                     'terms_version': '2026-11-01'},
    }


class GhlSpy:
    """Records every call the trial_ghl layer makes to outreach.ghl."""

    def __init__(self, *, fail=None):
        self.upserts = []
        self.added = []
        self.removed = []
        self.fail = fail

    def push_contact(self, payload):
        self.upserts.append(payload)
        if self.fail == 'upsert':
            from outreach.ghl import GHLError
            raise GHLError('GHL down')
        return {'contact': {'id': 'ghl_contact_1'}, 'traceId': 'trace1'}

    def add_tags(self, contact_id, tags):
        self.added.append((contact_id, list(tags)))
        if self.fail == 'tag':
            from outreach.ghl import GHLError
            raise GHLError('GHL tag failed')
        return {'tagsAdded': list(tags)}

    def remove_tags(self, contact_id, tags):
        self.removed.append((contact_id, list(tags)))
        return {}

    @property
    def tags_added(self):
        return [t for _cid, tags in self.added for t in tags]

    @property
    def tags_removed(self):
        return [t for _cid, tags in self.removed for t in tags]


def _patch_ghl(spy):
    """Patch the three outreach.ghl functions trial_ghl reaches for."""
    return (
        patch('outreach.ghl.push_contact', side_effect=spy.push_contact),
        patch('outreach.ghl.add_tags', side_effect=spy.add_tags),
        patch('outreach.ghl.remove_tags', side_effect=spy.remove_tags),
    )


@override_settings(STRIPE_WEBHOOK_SECRET='', DEBUG=True,
                   GHL_API_KEY='test-key', GHL_LOCATION_ID='loc_test',
                   SECURE_SSL_REDIRECT=False)
class TrialWebhookTests(TestCase):

    def setUp(self):
        _tier()
        _tier(slug=REVIEW_SLUG, name='Review System')
        self.url = reverse('billing:stripe_webhook')

    def _send(self, event, spy=None):
        spy = spy or GhlSpy()
        patches = _patch_ghl(spy)
        for p in patches:
            p.start()
        try:
            resp = self.client.post(
                self.url, json.dumps(event),
                content_type='application/json')
        finally:
            for p in patches:
                p.stop()
        return resp, spy

    # ── Idempotency ─────────────────────────────────────────────────

    def test_redelivery_is_skipped_entirely(self):
        _acceptance()
        event = _event('customer.subscription.created', _trialing_sub())

        resp, spy = self._send(event)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(spy.tags_added,
                         [f'trial-started-{VOICE_SLUG}'])

        # Same event id again: Stripe does this routinely.
        resp2, spy2 = self._send(event)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(spy2.tags_added, [],
                         'redelivery re-fired the GHL tag')
        self.assertEqual(spy2.upserts, [])
        self.assertEqual(
            StripeWebhookEvent.objects.filter(event_id='evt_test1').count(), 1)

    def test_a_different_event_id_is_not_skipped(self):
        """The guard must key on the event, not the event type."""
        _acceptance()
        resp, spy = self._send(_event('customer.subscription.created',
                                      _trialing_sub(), event_id='evt_a'))
        self.assertEqual(spy.tags_added, [f'trial-started-{VOICE_SLUG}'])
        _, spy2 = self._send(_event('customer.subscription.created',
                                    _trialing_sub(), event_id='evt_b'))
        self.assertEqual(spy2.tags_added, [f'trial-started-{VOICE_SLUG}'])

    def test_an_event_with_no_id_is_processed_without_a_ledger_row(self):
        """Load-bearing for the rest of the suite, so pinned here.

        Every real Stripe event carries an id, so the gate is always
        active in production. Most of the existing webhook tests build
        payloads with only the nested object's id and no top-level one,
        and several deliberately send the same payload twice to prove a
        handler is idempotent. If a missing id were treated as a skip,
        all of those would start passing without running their handler
        the second time: green, and testing nothing.
        """
        _acceptance()
        event = _event('customer.subscription.created', _trialing_sub())
        event.pop('id')

        _, spy = self._send(event)
        self.assertEqual(spy.tags_added, [f'trial-started-{VOICE_SLUG}'])
        self.assertFalse(StripeWebhookEvent.objects.exists())

        # And again: no ledger row means no skip.
        _, spy2 = self._send(event)
        self.assertEqual(spy2.tags_added, [f'trial-started-{VOICE_SLUG}'])

    def test_ledger_records_the_event_type(self):
        _acceptance()
        self._send(_event('customer.subscription.created', _trialing_sub()))
        row = StripeWebhookEvent.objects.get(event_id='evt_test1')
        self.assertEqual(row.event_type, 'customer.subscription.created')
        self.assertEqual(row.last_error, '')

    # ── trial started ───────────────────────────────────────────────

    def test_trial_started_upserts_then_tags(self):
        row = _acceptance()
        _, spy = self._send(_event('customer.subscription.created',
                                   _trialing_sub()))
        self.assertEqual(len(spy.upserts), 1)
        self.assertEqual(spy.tags_added, [f'trial-started-{VOICE_SLUG}'])
        # The contact id is kept for later events.
        row.refresh_from_db()
        self.assertEqual(row.ghl_contact_id, 'ghl_contact_1')

    def test_upsert_never_carries_a_tags_key(self):
        """The most expensive available mistake in this integration."""
        _acceptance()
        _, spy = self._send(_event('customer.subscription.created',
                                   _trialing_sub()))
        for payload in spy.upserts:
            self.assertNotIn('tags', payload,
                             'upsert would replace the whole tag array')

    def test_trial_started_sets_the_four_custom_fields_with_bare_keys(self):
        _acceptance()
        _, spy = self._send(_event('customer.subscription.created',
                                   _trialing_sub()))
        fields = {f['key']: f['fieldValue']
                  for f in spy.upserts[0]['customFields'] if 'key' in f}
        for key in ('trial_start', 'trial_end', 'stripe_customer_id',
                    'stripe_subscription_id'):
            self.assertIn(key, fields, f'{key} missing')
            self.assertNotIn(f'contact.{key}', fields)
        self.assertEqual(fields['stripe_customer_id'], 'cus_test123')
        self.assertEqual(fields['stripe_subscription_id'], 'sub_test123')
        # trial_end comes off the subscription as an ISO date, not a
        # unix timestamp: GHL date fields silently accept rubbish.
        self.assertRegex(fields['trial_end'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertRegex(fields['trial_start'], r'^\d{4}-\d{2}-\d{2}$')

    def test_name_is_split_on_the_first_space(self):
        user = User.objects.create_user(
            username='b@example.com', email='b@example.com', password='pw')
        from clients.account_models import Account
        account = Account.objects.filter(user=user).first() or \
            Account.objects.create(user=user, name='Cool Air HVAC')
        account.name = 'Cool Air HVAC'
        account.contact_name = 'Dave Van Moreno'
        account.phone = '2102010375'
        account.save()
        _acceptance()

        _, spy = self._send(_event('customer.subscription.created',
                                   _trialing_sub()))
        payload = spy.upserts[0]
        self.assertEqual(payload['firstName'], 'Dave')
        self.assertEqual(payload['lastName'], 'Van Moreno')
        # companyName is top-level, not a custom field.
        self.assertEqual(payload['companyName'], 'Cool Air HVAC')
        self.assertNotIn(
            'companyName',
            {f.get('key') for f in payload['customFields']})
        self.assertEqual(payload['phone'], '+12102010375')

    def test_a_charge_now_subscription_is_not_tagged_as_a_trial(self):
        sub = _trialing_sub()
        sub['metadata'].pop('trial_signup')
        sub['status'] = 'active'
        _acceptance()
        _, spy = self._send(_event('customer.subscription.created', sub))
        self.assertEqual(spy.tags_added, [])

    def test_trial_signup_metadata_but_not_trialing_is_not_tagged(self):
        """Defensive: the status is what makes it a trial."""
        sub = _trialing_sub()
        sub['status'] = 'active'
        _acceptance()
        _, spy = self._send(_event('customer.subscription.created', sub))
        self.assertEqual(spy.tags_added, [])

    # ── trial ending ────────────────────────────────────────────────

    def test_trial_will_end_adds_the_ending_tag(self):
        _acceptance()
        _, spy = self._send(_event('customer.subscription.trial_will_end',
                                   _trialing_sub()))
        self.assertEqual(spy.tags_added, [f'trial-ending-{VOICE_SLUG}'])

    # ── conversion ──────────────────────────────────────────────────

    def _paid_invoice(self, *, amount=9700, sub='sub_test123'):
        return {'id': 'in_test1', 'customer': 'cus_test123',
                'subscription': sub, 'amount_paid': amount}

    def test_first_real_invoice_tags_paid_and_clears_trial_tags(self):
        row = _acceptance()
        _, spy = self._send(_event('invoice.paid', self._paid_invoice()))
        self.assertEqual(spy.tags_added, [f'paid-{VOICE_SLUG}'])
        self.assertCountEqual(
            spy.tags_removed,
            [f'trial-started-{VOICE_SLUG}', f'trial-ending-{VOICE_SLUG}'])
        row.refresh_from_db()
        self.assertIsNotNone(row.converted_at)

    def test_the_zero_dollar_trial_invoice_does_not_convert(self):
        """Stripe marks the opening $0 trial invoice paid immediately."""
        row = _acceptance()
        _, spy = self._send(_event('invoice.paid',
                                   self._paid_invoice(amount=0)))
        self.assertEqual(spy.tags_added, [])
        row.refresh_from_db()
        self.assertIsNone(row.converted_at)

    def test_a_later_renewal_does_not_re_fire_conversion(self):
        """Month 13 must not re-tag or re-consume a cohort slot."""
        _acceptance(converted_at=timezone.now())
        _, spy = self._send(_event('invoice.paid', self._paid_invoice()))
        self.assertEqual(spy.tags_added, [])
        self.assertEqual(spy.tags_removed, [])

    def test_invoice_paid_for_an_unrelated_subscription_is_ignored(self):
        _acceptance(sub='sub_ours')
        _, spy = self._send(_event('invoice.paid',
                                   self._paid_invoice(sub='sub_someone_else')))
        self.assertEqual(spy.tags_added, [])

    def test_subscription_id_is_found_in_the_newer_invoice_shape(self):
        """Stripe moved it under parent.subscription_details."""
        _acceptance()
        invoice = {'id': 'in_test2', 'customer': 'cus_test123',
                   'amount_paid': 9700,
                   'parent': {'subscription_details':
                              {'subscription': 'sub_test123'}}}
        _, spy = self._send(_event('invoice.paid', invoice))
        self.assertEqual(spy.tags_added, [f'paid-{VOICE_SLUG}'])

    # ── cancellation and failure ────────────────────────────────────

    def test_subscription_deleted_adds_the_cancelled_tag(self):
        _acceptance()
        _, spy = self._send(_event('customer.subscription.deleted',
                                   {'id': 'sub_test123',
                                    'customer': 'cus_test123'}))
        self.assertEqual(spy.tags_added, [f'cancelled-{VOICE_SLUG}'])

    def test_payment_failed_adds_the_failure_tag(self):
        _acceptance()
        _, spy = self._send(_event('invoice.payment_failed',
                                   {'id': 'in_test3',
                                    'customer': 'cus_test123',
                                    'subscription': 'sub_test123',
                                    'amount_paid': 0}))
        self.assertEqual(spy.tags_added, [f'payment-failed-{VOICE_SLUG}'])

    def test_review_automation_tags_carry_its_own_slug(self):
        _acceptance(slug=REVIEW_SLUG, sub='sub_reviews',
                    email='r@example.com')
        sub = _trialing_sub(slug=REVIEW_SLUG, sub_id='sub_reviews')
        _, spy = self._send(_event('customer.subscription.created', sub))
        self.assertEqual(spy.tags_added, [f'trial-started-{REVIEW_SLUG}'])

    # ── Failure never costs a signup ────────────────────────────────

    def test_ghl_outage_records_a_failure_and_still_returns_200(self):
        from outreach.models import FailedLeadSubmission

        _acceptance()
        spy = GhlSpy(fail='upsert')
        resp, _ = self._send(_event('customer.subscription.created',
                                    _trialing_sub()), spy=spy)
        # Stripe must get a 200: a 500 makes it retry the whole event,
        # re-running everything else in the handler.
        self.assertEqual(resp.status_code, 200)
        failure = FailedLeadSubmission.objects.get()
        self.assertEqual(failure.payload['source'], 'billing.trial_ghl')
        self.assertEqual(failure.payload['email'], 'b@example.com')
        self.assertIn('GHL down', failure.error)

    def test_a_tag_failure_after_a_good_upsert_is_recorded(self):
        """Contact exists, workflow did not fire: the half-done state."""
        from outreach.models import FailedLeadSubmission

        _acceptance()
        spy = GhlSpy(fail='tag')
        resp, _ = self._send(_event('customer.subscription.created',
                                    _trialing_sub()), spy=spy)
        self.assertEqual(resp.status_code, 200)
        failure = FailedLeadSubmission.objects.get()
        self.assertEqual(failure.payload['stage'], 'tag')
        self.assertEqual(failure.payload['attempted']['contact_id'],
                         'ghl_contact_1')

    def test_missing_custom_fields_in_ghl_are_tolerated(self):
        """The four fields are created by hand and may not exist yet.

        GHL accepts the upsert and drops unknown keys. The TAG is what
        fires the workflow, so this is a degraded success.
        """
        _acceptance()
        _, spy = self._send(_event('customer.subscription.created',
                                   _trialing_sub()))
        self.assertEqual(spy.tags_added, [f'trial-started-{VOICE_SLUG}'])

    def test_no_acceptance_row_is_not_a_crash(self):
        resp, spy = self._send(_event('customer.subscription.created',
                                      _trialing_sub()))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(spy.tags_added, [])


@override_settings(STRIPE_WEBHOOK_SECRET='', DEBUG=True,
                   GHL_API_KEY='test-key', GHL_LOCATION_ID='loc_test',
                   SECURE_SSL_REDIRECT=False)
class TrialConversionCohortTests(TestCase):
    """Conversion is what moves the voice price ladder, not signup."""

    def setUp(self):
        _tier()
        self.url = reverse('billing:stripe_webhook')

    def _account(self, email='b@example.com'):
        from clients.account_models import Account
        user = User.objects.create_user(
            username=email, email=email, password='pw-123456')
        return (Account.objects.filter(user=user).first()
                or Account.objects.create(user=user, name='Cool Air'))

    def _send(self, event):
        spy = GhlSpy()
        patches = _patch_ghl(spy)
        for p in patches:
            p.start()
        try:
            return self.client.post(self.url, json.dumps(event),
                                    content_type='application/json'), spy
        finally:
            for p in patches:
                p.stop()

    def test_conversion_consumes_the_slot_and_leaves_price_locked(self):
        from voice.cohorts import current_cohort
        from voice.entitlement import program_for

        account = self._account()
        program = program_for(account, create=True, consume_slot=False)
        cohort = current_cohort()
        self.assertFalse(program.slot_consumed)
        self.assertEqual(cohort.filled, 0, 'a trial occupied a slot')
        locked_before = program.locked_price

        _acceptance()
        self._send(_event('invoice.paid',
                          {'id': 'in_1', 'customer': 'cus_test123',
                           'subscription': 'sub_test123',
                           'amount_paid': 9700}))

        program.refresh_from_db()
        self.assertTrue(program.slot_consumed)
        self.assertEqual(current_cohort().filled, 1)
        # Price was locked at signup and must not move on conversion.
        self.assertEqual(program.locked_price, locked_before)
        self.assertEqual(program.cohort_id, cohort.id)

    def test_a_redelivered_conversion_does_not_double_consume(self):
        from voice.cohorts import current_cohort
        from voice.entitlement import program_for

        account = self._account()
        program_for(account, create=True, consume_slot=False)
        _acceptance()
        event = _event('invoice.paid',
                       {'id': 'in_1', 'customer': 'cus_test123',
                        'subscription': 'sub_test123',
                        'amount_paid': 9700})
        self._send(event)
        self._send(event)
        self.assertEqual(current_cohort().filled, 1)

    def test_review_system_conversion_touches_no_cohort(self):
        _tier(slug=REVIEW_SLUG, name='Review System')
        account = self._account(email='r@example.com')
        _acceptance(slug=REVIEW_SLUG, sub='sub_r', email='r@example.com')
        resp, spy = self._send(_event(
            'invoice.paid',
            {'id': 'in_r', 'customer': 'cus_test123',
             'subscription': 'sub_r', 'amount_paid': 9700}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(spy.tags_added, [f'paid-{REVIEW_SLUG}'])
