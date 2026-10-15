"""Client-portal URLs for the Review System, mounted at /portal/reviews/."""

from django.urls import path

from reviews import views_portal as v

app_name = 'reviews'

urlpatterns = [
    path('', v.hub, name='hub'),
    path('manage/', v.manage, name='manage'),
    path('replies/', v.replies, name='replies'),
    path('signup/', v.signup, name='signup'),
    path('pause/', v.pause, name='pause'),
    path('resume/', v.resume, name='resume'),
    path('wording/', v.wording_save, name='wording_save'),
    path('settings/', v.settings_save, name='settings_save'),
]
