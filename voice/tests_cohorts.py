"""
The price ladder: $97 for 10 accounts, then +$50 per 10, grandfathered.

Three properties matter commercially and each one, if it broke, would
break quietly:

1. **Grandfathering.** An escalation must not change what an existing
   client pays. Stripe enforces this for the subscription itself, but
   ``locked_price`` is what every report and portal page reads, so it has
   to hold independently.
2. **Comps consume slots.** Owner's decision — comping two accounts
   leaves eight paying slots, not ten. If comps were invisible to the
   counter, the 11th customer would buy at the opening price.
3. **Re-seeding cannot reset the price.** ``seed_pricing`` is run on
   every deploy. An `update_or_create` that included `price` would drag a
   $197 tier back to $97 and start underselling by two rungs.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from billing.pricing_models import ServiceTier
from clients.account_models import Account
from clients.contract_options import VOICE_RECEPTIONIST_SLUG
from voice import cohorts as cohort_mod
from voice.entitlement import program_for
from voice.models import VoiceCohort, VoiceProgram

User = get_user_model()

_seq = 0


def _account(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vcoh{_seq}', email=f'vcoh{_seq}@example.com',
        password='test-pass-123')
    defaults = dict(user=user, name=f'Coh Co {_seq}',
                    onboarding_status='complete')
    defaults.update(kwargs)
    return Account.objects.create(**defaults)


def _tier():
    return ServiceTier.objects.get(slug=VOICE_RECEPTIONIST_SLUG)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class LadderTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_opening_cohort_exists_after_migrations(self):
        """The ladder is opened by voice/0002, not lazily on first use."""
        cohort = VoiceCohort.objects.get(sequence=1)
        self.assertTrue(cohort.is_current)
        self.assertEqual(cohort.price, Decimal('97.00'))
        self.assertEqual(cohort.capacity, 10)

    def test_slots_remaining_counts_down_as_programs_are_created(self):
        self.assertEqual(cohort_mod.slots_remaining(), 10)
        program_for(_account(), create=True)
        cache.clear()
        self.assertEqual(cohort_mod.slots_remaining(), 9)

    def test_comped_accounts_consume_slots(self):
        """Owner's decision: a comp is a slot. Comping yourself and one
        free client must leave eight paying slots, not ten."""
        for _ in range(2):
            account = _account(comp_voice_receptionist=True)
            program_for(account, create=True)
        cache.clear()
        self.assertEqual(cohort_mod.slots_remaining(), 8, (
            'comped accounts are not consuming cohort slots, so the 11th '
            'paying customer would get the opening price'))

    def test_tenth_account_escalates_and_repoints_the_tier(self):
        for _ in range(10):
            program_for(_account(), create=True)
        cache.clear()

        self.assertEqual(VoiceCohort.objects.count(), 2)
        nxt = VoiceCohort.objects.get(sequence=2)
        self.assertTrue(nxt.is_current)
        self.assertEqual(nxt.price, Decimal('147.00'))
        self.assertFalse(VoiceCohort.objects.get(sequence=1).is_current)

        self.assertEqual(_tier().price, Decimal('147.00'), (
            'the ServiceTier was not repointed, so the public pricing '
            'page is still quoting the closed cohort price'))

    def test_escalation_does_not_reprice_existing_clients(self):
        """The whole promise. Nine accounts at $97, then fill the cohort
        and make sure nobody moved."""
        early = [program_for(_account(), create=True) for _ in range(9)]
        for p in early:
            self.assertEqual(p.locked_price, Decimal('97.00'))

        program_for(_account(), create=True)  # 10th — triggers escalation
        cache.clear()
        self.assertEqual(_tier().price, Decimal('147.00'))

        for p in early:
            p.refresh_from_db()
            self.assertEqual(p.locked_price, Decimal('97.00'), (
                'an existing client was repriced by an escalation'))
            self.assertEqual(p.cohort.sequence, 1)

    def test_second_escalation_steps_again(self):
        for _ in range(20):
            program_for(_account(), create=True)
        cache.clear()
        self.assertEqual(VoiceCohort.objects.count(), 3)
        self.assertEqual(
            VoiceCohort.objects.get(sequence=3).price, Decimal('197.00'))
        self.assertEqual(_tier().price, Decimal('197.00'))

    def test_assign_to_current_is_idempotent(self):
        """A comp toggled off and back on must not reprice the client."""
        account = _account()
        program = program_for(account, create=True)
        original = program.cohort_id
        cohort_mod.assign_to_current(program)
        program.refresh_from_db()
        self.assertEqual(program.cohort_id, original)

    def test_full_cohort_hides_the_scarcity_bar_rather_than_saying_zero(self):
        """Between filling and escalating there is a window where the
        cohort is full. Advertising '0 spots left at $97' there would be
        wrong in both directions, so the bar must disappear."""
        cohort = VoiceCohort.objects.get(sequence=1)
        for _ in range(10):
            VoiceProgram.objects.create(account=_account(), cohort=cohort,
                                        locked_price=cohort.price)
        cache.clear()
        self.assertEqual(cohort_mod.cohort_context(), {})

    def test_opening_cohort_adopts_the_tier_price_id_once_it_exists(self):
        """voice/0002 opens cohort 1 at deploy time, before
        sync_stripe_products has ever run, so it copies a blank price id.
        Without a backfill the first rung has no record of what it sold
        at, and after an escalation there is no way to tell which Stripe
        Price the grandfathered clients are on."""
        cohort = VoiceCohort.objects.get(sequence=1)
        cohort.stripe_price_id = ''
        cohort.save(update_fields=['stripe_price_id'])

        tier = _tier()
        tier.stripe_price_id = 'price_live_abc123'
        tier.save(update_fields=['stripe_price_id'])

        refreshed = cohort_mod.current_cohort(create=False)
        self.assertEqual(refreshed.stripe_price_id, 'price_live_abc123')

    def test_adoption_never_rewrites_a_cohort_that_has_a_price(self):
        """After an escalation the tier points at the NEXT rung's price.
        Adopting that onto a closed rung would rewrite history and
        mis-state what existing clients are grandfathered on."""
        cohort = VoiceCohort.objects.get(sequence=1)
        cohort.stripe_price_id = 'price_original'
        cohort.save(update_fields=['stripe_price_id'])

        tier = _tier()
        tier.stripe_price_id = 'price_for_cohort_two'
        tier.save(update_fields=['stripe_price_id'])

        refreshed = cohort_mod.current_cohort(create=False)
        self.assertEqual(refreshed.stripe_price_id, 'price_original')

    def test_adoption_is_a_noop_when_the_tier_has_no_price_either(self):
        cohort = VoiceCohort.objects.get(sequence=1)
        cohort.stripe_price_id = ''
        cohort.save(update_fields=['stripe_price_id'])
        self.assertEqual(
            cohort_mod.current_cohort(create=False).stripe_price_id, '')

    def test_cohort_context_names_the_next_price(self):
        ctx = cohort_mod.cohort_context()
        self.assertEqual(ctx['slots_remaining'], 10)
        self.assertEqual(ctx['price'], Decimal('97.00'))
        self.assertEqual(ctx['next_price'], Decimal('147.00'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ReseedDoesNotResetPriceTests(TestCase):
    """seed_pricing runs on every deploy. It must refresh copy without
    undoing escalations."""

    def setUp(self):
        cache.clear()

    def test_reseed_preserves_an_escalated_price(self):
        tier = _tier()
        tier.price = Decimal('197.00')
        tier.save(update_fields=['price'])

        call_command('seed_pricing')

        self.assertEqual(_tier().price, Decimal('197.00'), (
            'seed_pricing reset a cohort-priced tier to its opening '
            'price — every new sale would be two rungs underpriced'))

    def test_reseed_still_refreshes_copy_and_features(self):
        tier = _tier()
        tier.tagline = 'stale tagline'
        tier.save(update_fields=['tagline'])
        tier.features.all().delete()

        call_command('seed_pricing')

        refreshed = _tier()
        self.assertNotEqual(refreshed.tagline, 'stale tagline')
        self.assertTrue(refreshed.features.exists())

    def test_first_seed_on_an_empty_db_still_sets_the_opening_price(self):
        """The guard keys on the row already existing, so a genuinely
        first seed must not be skipped."""
        ServiceTier.objects.filter(slug=VOICE_RECEPTIONIST_SLUG).delete()
        call_command('seed_pricing')
        self.assertEqual(_tier().price, Decimal('97.00'))
