from django.apps import AppConfig


class OnboardingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'onboarding'
    verbose_name = 'Onboarding wizard'

    def ready(self):
        # Import signals so the auto-completion hook is wired
        from . import signals  # noqa: F401
