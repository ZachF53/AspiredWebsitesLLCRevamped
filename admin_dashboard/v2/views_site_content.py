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

# Eight sections, one visible at a time behind a section rail. They used
# to render as eight cards down a single page, which is 35 fields of
# scroll whatever the spacing — the problem is the volume, not the gaps.
#
# `where` is the point of each section: every field here gates a block of
# the public site, and the only question worth answering before you type
# is "which page does this change?". It is rendered under the section
# title, not buried in a title parenthesis.
FIELD_GROUPS = (
    {
        'key': 'build',
        'title': 'Build scope',
        'where': 'Shows as "What the $2,000 build includes" on /pricing/. '
                 'Blank hides the whole list.',
        'fields': ('build_scope',),
    },
    {
        'key': 'reviews',
        'title': 'Review automation',
        'where': 'Fills the "How it connects" section of '
                 '/services/review-automation/.',
        'fields': (
            'review_platforms', 'review_manual_trigger', 'review_channel',
            'review_message_costs', 'review_consent_model', 'review_opt_out',
            'review_multi_location', 'review_setup_steps',
            'review_live_at_launch', 'review_reporting',
            'review_sample_message',
        ),
    },
    {
        'key': 'continuity',
        'title': 'Continuity and support',
        'where': 'Fine print on /about/ and /pricing/.',
        'fields': ('continuity_backups', 'continuity_outage_response'),
    },
    {
        'key': 'founding',
        'title': 'Founding-client offer',
        'where': 'Appears on /portfolio/ and /pricing/. Stays hidden until '
                 'it is both enabled and filled in.',
        'fields': ('founding_client_enabled', 'founding_client_headline',
                   'founding_client_offer', 'founding_client_ask'),
    },
    {
        'key': 'demo',
        'title': 'Demonstration build',
        'where': 'Appears on /portfolio/, always labelled "not a client '
                 'site".',
        'fields': ('demo_enabled', 'demo_title', 'demo_url',
                   'demo_description'),
    },
    {
        'key': 'proof',
        'title': 'Proof links and contact',
        'where': 'Google reviews link, CISSP number and whether the listed '
                 'phone accepts texts.',
        'fields': ('google_reviews_url', 'cissp_member_number',
                   'phone_accepts_sms'),
    },
    {
        'key': 'retention',
        'title': 'Retention periods',
        'where': 'Privacy policy. Left blank, the sentence is omitted '
                 'rather than guessed at.',
        'fields': ('session_recording_retention_days', 'audit_retention_days'),
    },
    {
        'key': 'faqs',
        'title': 'Policy FAQs',
        'where': 'The policy questions on /pricing/. A blank answer hides '
                 'that question.',
        'fields': ('faq_finance_with_hosting', 'faq_url_migration',
                   'faq_client_time', 'faq_travel_fee', 'faq_pause_plan'),
    },
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

    groups = [_build_group(spec, form) for spec in FIELD_GROUPS]
    # Errors win over everything: land on the section that needs fixing
    # rather than on a tab that looks fine while the message says
    # nothing was saved.
    active = next((g for g in groups if g['error_count']), groups[0])
    return render(request, 'admin_dashboard/v2/site_content.html', {
        'form': form,
        'groups': groups,
        'active_key': active['key'],
        'answered_total': sum(g['answered'] for g in groups),
        'field_total': sum(g['total'] for g in groups),
        'row': row,
    })


def _build_group(spec, form):
    """Bind one section spec to the form, with its own progress count.

    The count is what makes the rail useful: with one section on screen
    the other seven are invisible, so each needs to say at a glance
    whether anything is still owed.
    """
    fields = [form[name] for name in spec['fields']]
    return {
        'key': spec['key'],
        'title': spec['title'],
        'where': spec['where'],
        'fields': fields,
        'total': len(fields),
        'answered': sum(1 for f in fields if _is_answered(f)),
        'error_count': sum(1 for f in fields if f.errors),
    }


def _is_answered(bound_field):
    """True when this field carries a real answer.

    A checkbox is only counted when ticked. That is deliberate: every
    boolean here gates a public block (`founding_client_enabled`,
    `demo_enabled`, `phone_accepts_sms`), so unticked means the section
    is off, not that the question has been dealt with.
    """
    value = bound_field.value()
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return bool(str(value).strip())
