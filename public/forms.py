from django import forms

from outreach.models import Lead


class ContactForm(forms.Form):
    """
    Public-facing contact form. Saves to a Lead row with source='contact_form'
    per CLAUDE.md → Data Model Decisions → Contact Form → Lead Mapping.

    Sept 2026 — trimmed to name/phone/email/message. Business name,
    business type, "what do you need" and "how did you hear about us"
    were pre-pivot fields: business type offered Law Firm/Restaurant/
    Retail/etc. choices that don't apply now that the site sells to
    HVAC contractors only, and every visitor here is implicitly that
    audience already. save_as_lead() sets Lead.business_type='HVAC'
    directly rather than asking the visitor to pick it from a list of
    verticals that no longer describes what's sold.

      name    → Lead.attorney_name
      phone   → Lead.phone
      email   → Lead.email
      message → Lead.inquiry_text
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

    def save_as_lead(self, ip_address=None, referral_code=''):
        """Map cleaned form data to a Lead row and return it."""
        cleaned = self.cleaned_data
        return Lead.objects.create(
            firm_name='',
            attorney_name=cleaned['name'],
            business_type='HVAC',
            phone=cleaned['phone'],
            email=cleaned['email'],
            inquiry_text=cleaned['message'],
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
