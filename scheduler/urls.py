from django.urls import path

from . import views

app_name = 'scheduler'

urlpatterns = [
    path('schedule/', views.schedule_page, name='schedule_page'),
    # Per spec, the public-facing URL is /design/schedule/.
    # Three parallel routes — same calendar, service-specific form copy.
    path('design/schedule/', views.schedule_page,
         {'service': 'web_design'}, name='design_schedule'),
    path('social/schedule/', views.schedule_page,
         {'service': 'social_media'}, name='social_schedule'),
    path('seo/schedule/', views.schedule_page,
         {'service': 'seo'}, name='seo_schedule'),
    path('schedule/slots.json', views.slots_api, name='slots_api'),
    path('schedule/hold/', views.hold_slot, name='hold_slot'),
    path('schedule/confirm/', views.confirm_slot, name='confirm_slot'),
]
