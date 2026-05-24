from django import forms

from outreach.models import Lead


# Form-local choices for the contact form. Values double as display labels
# (no get_X_display needed) so what's stored on the Lead reads cleanly in
# email notifications, admin, and the CRM.
BUSINESS_TYPE_CHOICES = [
    ('Law Firm', 'Law Firm'),
    ('Restaurant', 'Restaurant'),
    ('Contractor', 'Contractor'),
    ('Retail', 'Retail'),
    ('Healthcare', 'Healthcare'),
    ('Technology', 'Technology'),
    ('Other', 'Other'),
]

HEARD_ABOUT_CHOICES = [
    ('Google Search', 'Google Search'),
    ('Referral', 'Referral'),
    ('Social Media', 'Social Media'),
    ('Cold Email', 'Cold Email'),
    ('Other', 'Other'),
]


class ContactForm(forms.Form):
    """
    Public-facing contact form. Saves to a Lead row with source='contact_form'
    per CLAUDE.md → Data Model Decisions → Contact Form → Lead Mapping.

    Field names match the original Phase 1 form (so the template doesn't
    need to change), but the save method maps them to the new Lead schema:
      name          → Lead.attorney_name
      business_name → Lead.firm_name
      business_type → Lead.business_type
      phone         → Lead.phone
      email         → Lead.email
      source        → Lead.tags  (how they heard about us)
      message       → Lead.inquiry_text
    """

    name = forms.CharField(
        label='Full Name',
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Jane Smith',
            'autocomplete': 'name',
        }),
    )

    business_name = forms.CharField(
        label='Business Name',
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Smith & Co.',
            'autocomplete': 'organization',
        }),
    )

    business_type = forms.ChoiceField(
        label='Business Type',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    phone = forms.CharField(
        label='Phone',
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'type': 'tel',
            'placeholder': '(210) 555-1234',
            'autocomplete': 'tel',
            'inputmode': 'tel',
            'maxlength': '14',
        }),
    )

    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'jane@business.com',
            'autocomplete': 'email',
            'autocapitalize': 'none',
            'autocorrect': 'off',
            'spellcheck': 'false',
            'inputmode': 'email',
        }),
    )

    def clean_phone(self):
        from core.phone_utils import normalize_phone
        return normalize_phone(self.cleaned_data.get('phone'))

    def clean_email(self):
        return (self.cleaned_data.get('email') or '').strip().lower()

    source = forms.ChoiceField(
        label='How did you hear about us?',
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    message = forms.CharField(
        label='Message',
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'placeholder': 'Tell us about your business and what you need.',
            'rows': 5,
        }),
    )

    # ── Spam-trap fields (no validation, just plumbing) ───────────────
    # Honeypot: real users never see this; bots that scan the DOM and
    # fill every input will tag themselves. Validated in the view.
    website_url = forms.CharField(required=False)
    # Signed timestamp stamped at render so the view can reject sub-3-
    # second "instant fill" submissions. Real value is set by the view
    # when it builds the form for GET.
    form_timestamp = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['business_type'].choices = (
            [('', '— Select business type —')] + BUSINESS_TYPE_CHOICES
        )
        self.fields['source'].choices = (
            [('', '— Optional —')] + HEARD_ABOUT_CHOICES
        )

    def save_as_lead(self, ip_address=None, referral_code=''):
        """Map cleaned form data to a Lead row and return it."""
        cleaned = self.cleaned_data
        return Lead.objects.create(
            firm_name=cleaned['business_name'],
            attorney_name=cleaned['name'],
            business_type=cleaned['business_type'],
            phone=cleaned['phone'],
            email=cleaned['email'],
            inquiry_text=cleaned['message'],
            tags=cleaned.get('source', ''),
            source='contact_form',
            status='new',
            score=0,
            ip_address=ip_address,
            referral_code=(referral_code or '').upper()[:20],
        )


class AuditForm(forms.Form):
    """Single-field form: visitor enters a URL to audit."""

    url = forms.URLField(
        label='Your Website URL',
        widget=forms.URLInput(attrs={
            'class': 'form-control',
            'placeholder': 'https://yourbusiness.com',
            'autocomplete': 'url',
            'inputmode': 'url',
            'required': True,
        }),
        error_messages={
            'invalid': 'Please enter a valid URL — e.g. https://yourbusiness.com',
            'required': 'Enter your website URL to get started.',
        },
    )


class AuditEmailForm(forms.Form):
    """Email capture on the audit results page."""

    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'you@business.com',
            'autocomplete': 'email',
            'required': True,
        }),
        error_messages={
            'invalid': 'Please enter a valid email address.',
            'required': 'Enter your email to receive the full report.',
        },
    )
