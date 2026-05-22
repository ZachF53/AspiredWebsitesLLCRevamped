"""
ASGI config for AspiredWebsitesRevamped.

HTTP is served by the standard Django application; WebSocket connections are
routed through Channels to the vault SSH terminal consumer.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'AspiredWebsitesRevamped.settings')

# Initialise Django (loads apps) BEFORE importing anything that touches models.
django_asgi_application = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

import vault.routing  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_application,
    'websocket': AuthMiddlewareStack(
        URLRouter(vault.routing.websocket_urlpatterns)
    ),
})
