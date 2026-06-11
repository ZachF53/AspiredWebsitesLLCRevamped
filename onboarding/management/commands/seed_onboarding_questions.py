"""
Seed the DB-backed onboarding registry (OnboardingSectionDef /
OnboardingQuestionDef) from the original Python definitions in
onboarding/registry.py.

Idempotent — re-running updates existing sections/questions in place
(keyed by product_type+section key and section+question key) and never
duplicates. Safe to run on every deploy. Does NOT touch any user's
answers or completion state.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from onboarding import registry as reg
from onboarding.question_models import (
    OnboardingQuestionDef, OnboardingSectionDef,
)


def _seed_question(section, q, order):
    OnboardingQuestionDef.objects.update_or_create(
        section=section, key=q['key'],
        defaults={
            'label': q.get('label', q['key']),
            'qtype': q.get('type', 'text'),
            'help': q.get('help', '') or '',
            'placeholder': q.get('placeholder', '') or '',
            'required': q.get('required', True),
            'skip_allowed': q.get('skip_allowed', True),
            'rows': q.get('rows'),
            'choices': [list(c) for c in q.get('choices', [])],
            'cred_category': q.get('cred_category', '') or '',
            'cred_type': q.get('cred_type', '') or '',
            'sort_order': order,
            'is_active': True,
        },
    )


def _seed_section(product_type, sec, order, **flags):
    section, _ = OnboardingSectionDef.objects.update_or_create(
        product_type=product_type, key=sec['key'],
        defaults={
            'title': sec.get('title', sec['key']),
            'intro': sec.get('intro', '') or '',
            'sort_order': order,
            'is_active': True,
            **flags,
        },
    )
    for i, q in enumerate(sec.get('questions', [])):
        _seed_question(section, q, i)
    return section


class Command(BaseCommand):
    help = 'Seed onboarding sections + questions from the Python registry.'

    @transaction.atomic
    def handle(self, *args, **opts):
        # ── Maintenance ──
        for i, sec in enumerate(reg._MAINTENANCE):
            flags = {}
            if sec['key'] == 'M5':
                flags['requires_hosting_moveover'] = True
            _seed_section('maintenance', sec, i, **flags)

        # ── Social media ── build the canonical 1-channel layout so S1
        # becomes a channel TEMPLATE (its per-channel questions are stored
        # un-indexed; the registry expands them per channel at runtime).
        social = reg._social_sections(1)
        for i, sec in enumerate(social):
            flags = {}
            questions = sec['questions']
            if sec['key'] == 'S1':
                flags['is_channel_template'] = True
                # Strip the channel_1_ / "Channel 1 — " prefixes so the
                # stored template is index-free and editable as one set.
                questions = []
                for q in sec['questions']:
                    qq = dict(q)
                    qq['key'] = q['key'].replace('channel_1_', '', 1)
                    qq['label'] = q['label'].replace('Channel 1 — ', '', 1)
                    questions.append(qq)
            if sec['key'] == 'S3':
                flags['tier_visibility'] = ['social-standard', 'social-full']
            if sec['key'] == 'S4':
                flags['skip_if_completed_intake'] = True
            _seed_section(
                'social_media', {**sec, 'questions': questions}, i, **flags)

        sec_n = OnboardingSectionDef.objects.count()
        q_n = OnboardingQuestionDef.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Seeded onboarding registry: {sec_n} sections, {q_n} questions.'))
