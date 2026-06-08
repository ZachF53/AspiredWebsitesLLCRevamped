"""
SetupTodo — post-onboarding "things we still need from you" tracker.

Each row is one outstanding task assigned to a user. The credential
auto-completion hook in `signals.py` flips a SetupTodo from pending
to completed when a matching VaultCredential is saved (matched by the
`credential_type` slug).

Lives in onboarding/todo_models.py rather than onboarding/models.py so
the registry can import from `todo_models` without circular imports
(registry uses these for the M5 / S4 conditional predicates).
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


TASK_TYPE_CHOICES = [
    ('vault_credential',   'Vault credential'),
    ('google_access',      'Google service access'),
    ('hosting_moveover',   'Hosting move-over'),
    ('manual',             'Manual task'),
]


class SetupTodo(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='setup_todos',
    )
    task_type = models.CharField(
        max_length=30, choices=TASK_TYPE_CHOICES, default='vault_credential',
    )
    # Matches VaultCredential.credential_type for auto-completion of
    # vault_credential tasks. Stays blank for non-credential tasks.
    credential_category = models.CharField(max_length=20, blank=True)
    credential_type = models.CharField(max_length=40, blank=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    deeplink_url = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=15, choices=[
            ('pending', 'Pending'),
            ('completed', 'Completed'),
        ],
        default='pending',
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    auto_completed_by = models.CharField(max_length=80, blank=True)
                          # e.g. "vault:42"

    reminder_3_sent = models.BooleanField(default=False)
    reminder_7_sent = models.BooleanField(default=False)
    reminder_14_sent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['status', '-created_at']

    def __str__(self):
        return f'{self.user} — {self.title} ({self.status})'

    def mark_completed(self, source=''):
        self.status = 'completed'
        self.completed_at = timezone.now()
        if source:
            self.auto_completed_by = source[:80]
        self.save(update_fields=[
            'status', 'completed_at', 'auto_completed_by'])


def build_todos_from_onboarding(onboarding):
    """
    Called from onboarding `complete()` view. Walks the onboarding's
    responses; for every `cred_access` question answered "share",
    create (or refresh) a matching SetupTodo.

    Idempotent — if a matching pending SetupTodo already exists for
    this user+credential_type, we don't duplicate it.
    """
    from onboarding.registry import visible_sections

    user = onboarding.user
    answers = {
        r.question_key: r for r in onboarding.responses.all()
    }

    for sec in visible_sections(onboarding):
        for q in sec['questions']:
            if q.get('type') != 'cred_access':
                continue
            r = answers.get(q['key'])
            if r is None or r.skipped:
                continue
            if r.value != 'share':
                continue
            cat = q.get('cred_category', 'other')
            typ = q.get('cred_type', 'other')
            existing = SetupTodo.objects.filter(
                user=user, task_type='vault_credential',
                credential_category=cat, credential_type=typ,
            ).first()
            if existing and existing.status == 'pending':
                continue
            if existing and existing.status == 'completed':
                continue  # already done
            # Need a client_id for the deeplink — find the user's first
            # ClientProfile or fall back to nothing.
            deeplink = ''
            try:
                from clients.models import ClientProfile
                cp = ClientProfile.objects.filter(user=user).first()
                if cp:
                    deeplink = (f'/admin-dashboard/vault/{cp.id}/add/?'
                                f'category={cat}&type={typ}')
            except Exception:
                pass
            SetupTodo.objects.create(
                user=user,
                task_type='vault_credential',
                credential_category=cat,
                credential_type=typ,
                title=q['label'],
                description=q.get('help', ''),
                deeplink_url=deeplink,
            )
