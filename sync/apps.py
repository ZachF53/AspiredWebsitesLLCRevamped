from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sync'

    def ready(self):
        # Connect the outbound stage-change signal handlers.
        from . import signals  # noqa: F401
