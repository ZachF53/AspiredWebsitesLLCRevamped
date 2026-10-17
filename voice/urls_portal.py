"""Client-portal URLs for the AI Voice Receptionist, at /portal/voice/."""

from django.urls import path

from voice import views_portal as v

app_name = 'voice'

urlpatterns = [
    path('', v.hub, name='hub'),
    path('manage/', v.manage, name='manage'),
    path('calls/', v.calls, name='calls'),
    path('signup/', v.signup, name='signup'),
    path('pause/', v.pause, name='pause'),
    path('resume/', v.resume, name='resume'),
    path('settings/', v.settings_save, name='settings_save'),
    path('knowledge/', v.knowledge_save, name='knowledge_save'),
]
