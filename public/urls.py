from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

app_name = 'public'

urlpatterns = [
    path('', views.home, name='home'),
    path('for-law-firms/', views.law_firms, name='law_firms'),
    path('portfolio/', views.portfolio, name='portfolio'),
    # Master Plan §11 — every project gets its own indexable URL.
    path('portfolio/<slug:slug>/', views.case_study_detail,
         name='case_study_detail'),
    path('pricing/', views.pricing, name='pricing'),

    # ── /insights/ — the blog (Master Plan §12) ────────────────────
    path('insights/', views.insights_index, name='insights'),
    path('insights/<slug:slug>/', views.insight_detail,
         name='insight_detail'),
    path('services/web-design/', views.service_web_design,
         name='service_web_design'),
    path('services/digital-marketing/', views.service_digital_marketing,
         name='service_digital_marketing'),
    path('services/seo/', views.service_seo, name='service_seo'),

    # ── Phase 2 service pages ──────────────────────────────────────
    # Ordered here by measured commercial value (Keyword Planner,
    # 2026-08-02). law-firm-seo is the highest-value page on the site:
    # 8,000 searches/mo at $31-165 top-of-page bids, because the
    # lifetime value is an SEO retainer rather than a one-off build.
    # See .claude/improvements/KEYWORD_RESEARCH_FINDINGS.md.
    path('services/seo/law-firm-seo/', views.service_law_firm_seo,
         name='service_law_firm_seo'),
    path('services/web-design/law-firm-web-design/',
         views.service_law_firm_web_design,
         name='service_law_firm_web_design'),
    path('services/seo/local-seo/', views.service_local_seo,
         name='service_local_seo'),
    path('services/web-design/small-business-web-design/',
         views.service_small_business_web_design,
         name='service_small_business_web_design'),
    path('services/web-design/website-redesign/',
         views.service_website_redesign,
         name='service_website_redesign'),

    # ── Phase 3 ────────────────────────────────────────────────────
    path('services/web-design/custom-web-development/',
         views.service_custom_web_development,
         name='service_custom_web_development'),
    # Location pages — D5, revised Aug 2026. Three now, not one:
    #   san-antonio   2,860/mo · three real clients there
    #   atlanta       2,160/mo · unblocked once the registered-agent
    #                 address left the schema and the homepage title
    #                 stopped competing for the term
    #   warner-robins ~10/mo · built for local signal, not traffic —
    #                 the only city where the site's own NAP and schema
    #                 say we are actually located, so it is the page a
    #                 service-area GBP can point at
    # Still no /locations/ index — a hub listing three links, with
    # nothing to say of its own, is the thin page §15 forbids.
    path('locations/san-antonio/', views.location_san_antonio,
         name='location_san_antonio'),
    path('locations/atlanta/', views.location_atlanta,
         name='location_atlanta'),
    path('locations/warner-robins/', views.location_warner_robins,
         name='location_warner_robins'),
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
