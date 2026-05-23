"""Public-facing legal-document URLs (Privacy Policy + Terms)."""

from django.urls import path

from . import views

app_name = 'core'

urlpatterns = [
    path('privacy-policy/', views.privacy_policy,
         name='privacy_policy'),
    path('terms/', views.terms_of_service,
         name='terms_of_service'),
]
