from django.apps import AppConfig


class ClientsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'clients'

    def ready(self):
        # Wire the auto-create Account+Website post_save signal so
        # every new ClientProfile (Stripe webhook, Moonieful sync,
        # admin create) materialises the new-model rows without a
        # manual refactor_to_accounts run.
        from . import signals  # noqa: F401

        # Guarantee that any row written with a legacy owner also gets
        # its canonical Account/Website FK. Writers routinely set the
        # legacy FK and forget the canonical one; the row then saves
        # cleanly but is invisible to every canonical reader. A backfill
        # only fixes rows that already exist — this stops new ones being
        # created unstamped.
        from . import canonical_stamping
        canonical_stamping.install()
