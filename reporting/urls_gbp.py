"""Phase 5a-pivot — GBP URL routes mounted at /admin-dashboard/gbp/."""

from django.urls import path

from reporting import gbp_oauth_views, views_gbp

app_name = 'gbp'

urlpatterns = [
    # Cross-client dashboard
    path('', views_gbp.dashboard, name='dashboard'),

    # OAuth (operator-level — single token shared across all clients)
    path('connect/', gbp_oauth_views.connect_page, name='connect_page'),
    path('connect/start/',
         gbp_oauth_views.connect_start, name='connect_start'),
    path('oauth/callback/',
         gbp_oauth_views.oauth_callback, name='oauth_callback'),
    path('disconnect/', gbp_oauth_views.disconnect, name='disconnect'),

    # Per-website deep dive. A GBP listing describes one business
    # location, so it binds to a site rather than to the account.
    path('websites/<uuid:website_id>/',
         views_gbp.client_gbp, name='client_gbp'),
    path('websites/<uuid:website_id>/bind/',
         views_gbp.locations_picker, name='locations_picker'),
    path('websites/<uuid:website_id>/reviews/',
         views_gbp.reviews_list, name='reviews_list'),
    path('websites/<uuid:website_id>/nap/',
         views_gbp.nap_history, name='nap_history'),
]
