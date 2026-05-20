from django.urls import path

from . import views

app_name = 'public'

urlpatterns = [
    path('', views.home, name='home'),
    path('for-law-firms/', views.law_firms, name='law_firms'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('pricing/', views.pricing, name='pricing'),
    path('contact/', views.contact, name='contact'),
    path('contact/thanks/', views.contact_thanks, name='contact_thanks'),
    path('about/', views.about, name='about'),
    path('audit/', views.audit, name='audit'),
    path('audit/results/', views.audit_results, name='audit_results'),
    path('audit/ai-review/', views.audit_ai_review, name='audit_ai_review'),
    path('login/', views.login_page, name='login'),
    path('portal/coming-soon/', views.portal_coming_soon, name='portal_coming_soon'),
]
