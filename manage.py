#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def default_settings_module(argv):
    """Choose a safe default without overriding an explicit environment."""
    command = argv[1] if len(argv) > 1 else ''
    if command == 'test':
        return 'AspiredWebsitesRevamped.settings_test'
    if command == 'runserver':
        return 'AspiredWebsitesRevamped.settings_development'
    # Production cron/management commands historically invoke manage.py
    # without --settings.  Preserve the environment-aware base module for
    # those commands; WSGI/ASGI/Celery use settings_production explicitly.
    return 'AspiredWebsitesRevamped.settings'


def main():
    """Run administrative tasks."""
    # Tests get isolated services automatically and runserver is always local
    # HTTP.  An explicitly supplied DJANGO_SETTINGS_MODULE still wins.
    os.environ.setdefault(
        'DJANGO_SETTINGS_MODULE', default_settings_module(sys.argv))
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
