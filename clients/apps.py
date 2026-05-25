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
