from django.urls import path

from . import views

app_name = 'onboarding'

urlpatterns = [
    path('', views.dispatch, name='dispatch'),
    path('todos/modal/', views.todo_modal, name='todo_modal'),
    path('todos/count.json', views.todo_count, name='todo_count'),
    path('<str:product_type>/<str:tier_slug>/welcome/',
         views.welcome, name='welcome'),
    path('<str:product_type>/<str:tier_slug>/complete/',
         views.complete, name='complete'),
    path('<str:product_type>/<str:tier_slug>/save/',
         views.save_answer, name='save'),
    path('<str:product_type>/<str:tier_slug>/skip/',
         views.skip_answer, name='skip'),
    path('<str:product_type>/<str:tier_slug>/<str:section_key>/',
         views.step, name='step'),
]
