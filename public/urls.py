from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

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
    path('logout/', views.logout_view, name='logout'),
    path('portal/coming-soon/', views.portal_coming_soon, name='portal_coming_soon'),

    # ── Password reset flow ────────────────────────────────────────────
    # Django's built-in views — we just supply our own templates so the
    # emails + pages match the Aspired brand. The 4-step flow:
    #   /password-reset/         → form, email-by-address
    #   /password-reset/sent/    → "check your email" page
    #   /password-reset/<uid>/<token>/ → set-new-password form (link from email)
    #   /password-reset/done/    → "your password is changed" page
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='public/password_reset_form.html',
            email_template_name='public/password_reset_email.txt',
            subject_template_name='public/password_reset_subject.txt',
            success_url=reverse_lazy('public:password_reset_done'),
        ),
        name='password_reset',
    ),
    path(
        'password-reset/sent/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='public/password_reset_done.html',
        ),
        name='password_reset_done',
    ),
    path(
        'password-reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='public/password_reset_confirm.html',
            success_url=reverse_lazy('public:password_reset_complete'),
        ),
        name='password_reset_confirm',
    ),
    path(
        'password-reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='public/password_reset_complete.html',
        ),
        name='password_reset_complete',
    ),

    # Domain parking page — destination for cancelled-hosting domains
    # whose DNS has been re-pointed via URL301 to here. Called by
    # `domains.services.park_domain`.
    path('parked/', views.domain_parked, name='domain_parked'),
]
