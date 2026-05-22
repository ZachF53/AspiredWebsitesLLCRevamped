from django.apps import AppConfig


class VaultAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vault'

    def ready(self):
        # Connect the ClientProfile -> ClientVault auto-create signal.
        from . import signals  # noqa: F401
