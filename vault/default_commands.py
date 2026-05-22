"""Default ServerCommandLibrary entries seeded for new SSH credentials."""

DEFAULT_SSH_COMMANDS = [
    {
        'label': 'Check all services',
        'command': 'supervisorctl status',
        'category': 'monitoring',
        'requires_confirmation': False,
        'is_dangerous': False,
        'sort_order': 1,
    },
    {
        'label': 'Restart Gunicorn',
        'command': 'supervisorctl restart aspiredwebsites',
        'category': 'maintenance',
        'requires_confirmation': True,
        'is_dangerous': True,
        'sort_order': 2,
    },
    {
        'label': 'Restart Celery',
        'command': 'supervisorctl restart aspiredwebsites-celery',
        'category': 'maintenance',
        'requires_confirmation': True,
        'is_dangerous': False,
        'sort_order': 3,
    },
    {
        'label': 'Restart Nginx',
        'command': 'systemctl restart nginx',
        'category': 'maintenance',
        'requires_confirmation': True,
        'is_dangerous': True,
        'sort_order': 4,
    },
    {
        'label': 'Tail Gunicorn errors',
        'command': 'tail -50 /var/www/aspired/logs/gunicorn-error.log',
        'category': 'logs',
        'requires_confirmation': False,
        'is_dangerous': False,
        'sort_order': 5,
    },
    {
        'label': 'Tail access log',
        'command': 'tail -50 /var/www/aspired/logs/gunicorn-access.log',
        'category': 'logs',
        'requires_confirmation': False,
        'is_dangerous': False,
        'sort_order': 6,
    },
    {
        'label': 'Check disk space',
        'command': 'df -h /',
        'category': 'monitoring',
        'requires_confirmation': False,
        'is_dangerous': False,
        'sort_order': 7,
    },
    {
        'label': 'Check memory',
        'command': 'free -h',
        'category': 'monitoring',
        'requires_confirmation': False,
        'is_dangerous': False,
        'sort_order': 8,
    },
    {
        'label': 'Check uptime',
        'command': 'uptime',
        'category': 'monitoring',
        'requires_confirmation': False,
        'is_dangerous': False,
        'sort_order': 9,
    },
    {
        'label': 'Run deploy.sh',
        'command': 'bash /var/www/aspired/app/deploy.sh',
        'category': 'deploy',
        'requires_confirmation': True,
        'is_dangerous': False,
        'sort_order': 10,
    },
]


def create_default_commands(credential):
    """
    Seed the default ServerCommandLibrary entries for an SSH credential.
    Idempotent — get_or_create keyed on (credential, label).
    """
    from .models import ServerCommandLibrary
    for cmd in DEFAULT_SSH_COMMANDS:
        ServerCommandLibrary.objects.get_or_create(
            credential=credential,
            label=cmd['label'],
            defaults=cmd,
        )
