"""
Phase 5a — Social Manager URL routes.

Mounted at /admin-dashboard/social/ in the root urls.py.

  /admin-dashboard/social/                              channels list
  /admin-dashboard/social/<uuid>/connect/               OAuth status
  /admin-dashboard/social/<uuid>/connect/start/         kick off OAuth
  /admin-dashboard/social/oauth/google/callback/        Google callback
  /admin-dashboard/social/<uuid>/disconnect/            POST drop token
  /admin-dashboard/social/<uuid>/locations/             pick GBP location
  /admin-dashboard/social/<uuid>/compose/               compose / draft
  /admin-dashboard/social/<uuid>/posts/                 channel posts
"""

from django.urls import path

from social import google_oauth_views, views

app_name = 'social'

urlpatterns = [
    path('', views.channels_list, name='channels_list'),
    # OAuth flow
    path('<uuid:channel_id>/connect/',
         views.connect_page, name='connect_page'),
    path('<uuid:channel_id>/connect/start/',
         google_oauth_views.connect_start, name='connect_start'),
    path('oauth/google/callback/',
         google_oauth_views.oauth_callback, name='oauth_callback'),
    path('<uuid:channel_id>/disconnect/',
         google_oauth_views.disconnect, name='disconnect'),
    # Location binding (per-channel)
    path('<uuid:channel_id>/locations/',
         views.locations_picker, name='locations_picker'),
    # Composer + list
    path('<uuid:channel_id>/compose/',
         views.post_composer, name='post_composer'),
    path('<uuid:channel_id>/posts/',
         views.posts_list, name='posts_list'),
]
