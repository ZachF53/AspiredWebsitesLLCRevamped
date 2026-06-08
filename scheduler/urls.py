from django.urls import path

from . import views

app_name = 'scheduler'

urlpatterns = [
    path('schedule/', views.schedule_page, name='schedule_page'),
    # Per spec, the public-facing URL is /design/schedule/
    path('design/schedule/', views.schedule_page, name='design_schedule'),
    path('schedule/slots.json', views.slots_api, name='slots_api'),
    path('schedule/hold/', views.hold_slot, name='hold_slot'),
    path('schedule/confirm/', views.confirm_slot, name='confirm_slot'),
]
