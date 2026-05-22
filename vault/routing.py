"""Channels WebSocket routing for the vault SSH terminal."""

from django.urls import re_path

from vault import consumers

websocket_urlpatterns = [
    re_path(
        r'ws/ssh/(?P<cred_id>[0-9a-fA-F-]+)/$',
        consumers.SSHTerminalConsumer.as_asgi(),
    ),
]
