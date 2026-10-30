from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Probe the cache once per process so a boot against a dead Redis
        # is reported at startup rather than on whichever request first
        # happens to touch the cache. See core/cache_health.py.
        #
        # Deliberately not fatal and deliberately quiet on success: it
        # logs one INFO line when healthy and an ERROR when not. Under
        # settings_test / settings_rehearsal the backend is LocMemCache,
        # which the probe skips.
        from core.cache_health import probe_cache
        probe_cache()
