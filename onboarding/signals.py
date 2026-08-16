"""
Auto-completion hook for SetupTodo.

When a VaultCredential is saved with a `credential_type` other than
'other', look for an open SetupTodo with the matching slug for the
same user and flip it to completed.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='vault.VaultCredential')
def auto_complete_matching_todo(sender, instance, created, **kwargs):
    if kwargs.get('raw'):
        return  # fixture load — don't complete todos from restored rows
    if not instance.credential_type or instance.credential_type == 'other':
        return
    try:
        from onboarding.todo_models import SetupTodo
    except Exception:
        return

    # Resolve the credential's user via its vault → ClientProfile
    user = None
    try:
        cp = instance.vault.client
        user = getattr(cp, 'user', None)
    except Exception:
        return
    if user is None:
        return

    pending = SetupTodo.objects.filter(
        user=user,
        task_type='vault_credential',
        credential_type=instance.credential_type,
        status='pending',
    )
    for todo in pending:
        todo.mark_completed(source=f'vault:{instance.pk}')
        logger.info(
            'auto-completed SetupTodo %s from vault cred %s',
            todo.pk, instance.pk)
