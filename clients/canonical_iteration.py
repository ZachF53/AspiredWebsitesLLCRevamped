"""
Iterating clients during the cutover, without losing any.

Scheduled work across the codebase walks the legacy table:

    for client in ClientProfile.objects.filter(status='active'):
        ...

Every one of those loops has the same latent fault. An Account with no
legacy ClientProfile — the shape every account created after the cutover
takes — is simply not in the queryset, so the task skips it and reports
success. No health score, no monthly report, no onboarding reminder, no
error. The client is invisible to the scheduler.

Converting each loop to iterate Websites or Accounts is the real fix, but
the loops feed helpers that still take a ClientProfile, so they cannot all
move at once. This module is the intermediate step: it yields exactly what
the old queryset yielded, and separately reports the accounts that were
dropped, so the gap is loud instead of silent.

Behaviour today is identical — every production account still has a legacy
profile. The value is that the day one does not, the scheduler says so
instead of quietly doing less work.
"""

import logging

logger = logging.getLogger(__name__)


def profiles_with_coverage_report(task_name, **filters):
    """Yield ClientProfiles matching `filters`, warning about the gap.

    `filters` are applied to ClientProfile exactly as before, so callers
    keep their existing semantics. Accounts that hold no legacy profile
    are counted and reported once per run rather than per row.
    """
    from clients.account_models import Account
    from clients.models import ClientProfile

    queryset = ClientProfile.objects.filter(**filters)

    skipped = list(
        Account.objects.filter(legacy_client_profile__isnull=True)
        .values_list('pk', 'name')[:20]
    )
    if skipped:
        _report_skipped(task_name, skipped)

    return queryset


def _report_skipped(task_name, skipped):
    names = ', '.join(f'{name} ({pk})' for pk, name in skipped)
    logger.error(
        '%s: %d account(s) have no legacy ClientProfile and were skipped '
        'by this scheduled task: %s',
        task_name, len(skipped), names)
    try:
        from core.system_alerts import record_alert

        record_alert(
            severity='error',
            source=f'scheduler.skipped_accounts.{task_name}',
            message=(f'{task_name} skipped {len(skipped)} account(s) with '
                     'no legacy ClientProfile.'),
            detail=('These accounts are invisible to this scheduled task '
                    'because it iterates the legacy table. They received '
                    f'no work this run: {names}'),
        )
    except Exception:
        logger.exception(
            '%s: could not record the skipped-account alert', task_name)
