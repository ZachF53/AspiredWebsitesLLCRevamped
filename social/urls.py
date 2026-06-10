"""URL routes for social media manager — mounted at
/admin-dashboard/social/."""

from django.urls import path

from . import linkedin_oauth, meta_oauth, views

app_name = 'social'

urlpatterns = [
    # Cross-client triage
    path('', views.channels_list, name='channels_list'),

    # Per-channel pages
    path('<uuid:channel_id>/connect/', views.connect_page,
         name='connect_page'),
    path('<uuid:channel_id>/compose/', views.post_composer,
         name='compose'),
    path('<uuid:channel_id>/posts/', views.posts_list,
         name='posts_list'),
    path('<uuid:channel_id>/posts/<uuid:post_id>/delete/',
         views.post_delete, name='post_delete'),

    # Meta OAuth flow
    path('<uuid:channel_id>/meta/start/', meta_oauth.connect_start,
         name='meta_connect_start'),
    path('meta/callback/', meta_oauth.oauth_callback,
         name='meta_oauth_callback'),
    path('<uuid:channel_id>/meta/picker/', meta_oauth.page_picker,
         name='meta_page_picker'),
    path('<uuid:channel_id>/meta/disconnect/', meta_oauth.disconnect,
         name='meta_disconnect'),

    # LinkedIn OAuth flow
    path('<uuid:channel_id>/linkedin/start/',
         linkedin_oauth.connect_start, name='linkedin_connect_start'),
    path('linkedin/callback/', linkedin_oauth.oauth_callback,
         name='linkedin_oauth_callback'),
    path('<uuid:channel_id>/linkedin/picker/',
         linkedin_oauth.org_picker, name='linkedin_org_picker'),
    path('<uuid:channel_id>/linkedin/disconnect/',
         linkedin_oauth.disconnect, name='linkedin_disconnect'),
]
