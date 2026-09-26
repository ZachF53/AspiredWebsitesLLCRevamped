"""
v2 Site Content — the owner-input switchboard for the public site.

Every field on public.models.SiteContent gates a public section that
must not render until the owner has supplied a true answer (build scope,
review-automation mechanics, continuity targets, founding-client offer,
demo build, CISSP number, retention periods, policy FAQs). Saving here
publishes immediately: SiteContent.save() clears its cache.
"""

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render

from admin_dashboard.decorators import admin_required
from public.models import SiteContent

# Grouped so the form reads as sections rather than one 35-field wall.
FIELD_GROUPS = (
    ('Build scope (shows "What the $2,000 build includes" on /pricing/)', (
        'build_scope',
    )),
    ('Review automation: "How it connects" (/services/review-automation/)', (
        'review_platforms', 'review_manual_trigger', 'review_channel',
        'review_message_costs', 'review_consent_model', 'review_opt_out',
        'review_multi_location', 'review_setup_steps', 'review_live_at_launch',
        'review_reporting', 'review_sample_message',
    )),
    ('Continuity and support (/about/ and /pricing/ fine print)', (
        'continuity_backups', 'continuity_outage_response',
    )),
    ('Founding-client offer (/portfolio/ and /pricing/; hidden until enabled and filled)', (
        'founding_client_enabled', 'founding_client_headline',
        'founding_client_offer', 'founding_client_ask',
    )),
    ('Demonstration HVAC build (/portfolio/; always labelled "not a client site")', (
        'demo_enabled', 'demo_title', 'demo_url', 'demo_description',
    )),
    ('Proof links and contact', (
        'google_reviews_url', 'cissp_member_number', 'phone_accepts_sms',
    )),
    ('Retention (privacy policy; blank omits the sentence)', (
        'session_recording_retention_days', 'audit_retention_days',
    )),
    ('Policy FAQs on /pricing/ (blank hides the question)', (
        'faq_finance_with_hosting', 'faq_url_migration', 'faq_client_time',
        'faq_travel_fee', 'faq_pause_plan',
    )),
)


class SiteContentForm(forms.ModelForm):
    class Meta:
        model = SiteContent
        exclude = ('updated_at',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                continue
            field.widget.attrs.setdefault('class', 'form-control')
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs['rows'] = 3


@admin_required
def site_content_edit(request):
    row = SiteContent.get_solo()
    # get_solo() may hand back a cached copy; edit the live row.
    row = SiteContent.objects.get(pk=row.pk)
    if request.method == 'POST':
        form = SiteContentForm(request.POST, instance=row)
        if form.is_valid():
            form.save()
            messages.success(request, 'Site content saved. Changes are live on the public site now.')
            return redirect('admin_dashboard:v2_site_content')
        messages.error(request, 'Nothing saved. Fix the highlighted fields.')
    else:
        form = SiteContentForm(instance=row)

    groups = [
        (title, [form[name] for name in names])
        for title, names in FIELD_GROUPS
    ]
    return render(request, 'admin_dashboard/v2/site_content.html', {
        'form': form,
        'groups': groups,
        'row': row,
    })
