from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy
from django.views.generic import RedirectView

from . import views

app_name = 'public'

# Sept 2026 repositioning — the site sells to HVAC contractors only now,
# so the law-firm/small-business/SEO/social service pages below are no
# longer sold as distinct products. Rather than delete them (real
# ranking history on some of these, per public/legacy_redirects.py's
# reasoning) or leave them live selling a product that doesn't exist
# anymore, each 301s to the HVAC web design page. Views and templates
# stay on disk, unreferenced by any URL — nothing here is deleted.
_RETIRED_TO_WEB_DESIGN = RedirectView.as_view(
    pattern_name='public:service_web_design', permanent=True)

# Plan M-3.04 (Sept 2026): local SEO as a service is discontinued, and the
# closest surviving intent for someone who searched for it (showing up in
# the map pack) is automated review generation, not the build page.
_RETIRED_TO_REVIEW_AUTOMATION = RedirectView.as_view(
    pattern_name='public:service_review_automation', permanent=True)

# Plan M-5.01: /portfolio/other/ merged into the single /portfolio/.
_MERGED_INTO_PORTFOLIO = RedirectView.as_view(
    pattern_name='public:portfolio', permanent=True)

urlpatterns = [
    path('', views.home, name='home'),
    path('for-law-firms/', _RETIRED_TO_WEB_DESIGN, name='law_firms'),
    path('portfolio/', views.portfolio, name='portfolio'),
    # Merged into /portfolio/ (plan M-5.01); 301. Must come before the
    # <slug:slug> pattern below, or '/portfolio/other/' would match it
    # first with slug='other' and 404 as a case study that doesn't exist.
    path('portfolio/other/', _MERGED_INTO_PORTFOLIO, name='portfolio_other'),
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
    path('services/review-automation/', views.service_review_automation,
         name='service_review_automation'),
    path('services/hosting-maintenance/', views.service_hosting_maintenance,
         name='service_hosting_maintenance'),
    path('services/hosting-maintenance/sample-report/',
         views.sample_security_report, name='sample_security_report'),
    path('services/digital-marketing/', _RETIRED_TO_WEB_DESIGN,
         name='service_digital_marketing'),
    path('services/seo/', _RETIRED_TO_WEB_DESIGN, name='service_seo'),

    # ── Phase 2 service pages — retired Sept 2026, see
    # _RETIRED_TO_WEB_DESIGN above. Names kept so every existing
    # {% url %} reference still resolves; each now 301s. ────────────
    path('services/seo/law-firm-seo/', _RETIRED_TO_WEB_DESIGN,
         name='service_law_firm_seo'),
    path('services/web-design/law-firm-web-design/',
         _RETIRED_TO_WEB_DESIGN,
         name='service_law_firm_web_design'),
    path('services/seo/local-seo/', _RETIRED_TO_REVIEW_AUTOMATION,
         name='service_local_seo'),
    path('services/web-design/small-business-web-design/',
         _RETIRED_TO_WEB_DESIGN,
         name='service_small_business_web_design'),
    path('services/web-design/website-redesign/',
         _RETIRED_TO_WEB_DESIGN,
         name='service_website_redesign'),

    # ── Phase 3 — retired Sept 2026 ──────────────────────────────────
    path('services/web-design/custom-web-development/',
         _RETIRED_TO_WEB_DESIGN,
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
    # One generic view (location_city) serves all three — each keeps its
    # own literal path and name= so every existing URL and {% url %}
    # reference (sitemaps.py, base.html footer, legacy_redirects.py,
    # case_study_detail.html) resolves exactly where it always did. The
    # per-city copy that used to be hardcoded in three templates now
    # lives on the City model (see public/views.py location_city).
    path('locations/san-antonio/', views.location_city,
         {'slug': 'san-antonio'}, name='location_san_antonio'),
    path('locations/atlanta/', views.location_city,
         {'slug': 'atlanta'}, name='location_atlanta'),
    path('locations/warner-robins/', views.location_city,
         {'slug': 'warner-robins'}, name='location_warner_robins'),
    path('contact/', views.contact, name='contact'),
    path('contact/thanks/', views.contact_thanks, name='contact_thanks'),
    path('callback/', views.callback_request, name='callback'),
    path('about/', views.about, name='about'),
    path('audit/', views.audit, name='audit'),
    path('audit/results/', views.audit_results, name='audit_results'),
    # One-click opt-out from the audit follow-up sequence. No
    # login: the token is signed and identifies the record.
    path('audit/unsubscribe/<str:token>/', views.audit_unsubscribe,
         name='audit_unsubscribe'),
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
