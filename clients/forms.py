"""Forms for the client portal."""

from django import forms

from .models import (
    ClientDocument,
    ClientProfile,
    IntakeResponse,
    RevisionRequest,
    SupportTicket,
)


class IntakeForm(forms.ModelForm):
    """The full intake questionnaire — every field is optional so the form
    can be saved partially as the client works through the steps.

    Notes:
      - `google_business_access` is intentionally NOT exposed here; the
        old "I've granted access" checkbox was misplaced (clients don't
        have a reason to grant access before the build starts). It moves
        to a post-launch operations task.
      - Social profiles are split into four standard URL fields plus a
        catch-all textarea. The freeform `social_links` blob is preserved
        for "anything else".
      - `domain_registrar_other` is rendered conditionally by the
        template's JS when `domain_registrar` is set to "Other".
    """

    class Meta:
        model = IntakeResponse
        fields = [
            'brand_colors', 'brand_fonts', 'logo', 'no_logo_yet',
            'photos_provided', 'photos_note',
            'about_copy', 'practice_areas', 'attorney_bios',
            'reference_sites', 'competitors',
            'domain_name', 'domain_registrar', 'domain_registrar_other',
            'facebook_url', 'instagram_url', 'linkedin_url',
            'twitter_url', 'google_business_url', 'social_links',
        ]
        widgets = {
            'brand_colors': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. navy blue, gold, white',
            }),
            'brand_fonts': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Serif headings, sans-serif body',
            }),
            'logo': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'photos_note': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': 'Notes about photos — what to use, what to avoid, anyone you don\'t want pictured, etc.',
            }),
            'about_copy': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 5,
                'placeholder': 'Your story, what makes you different, why clients should trust you. Don\'t worry about polish — we\'ll edit.',
            }),
            'practice_areas': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': 'One per line. A sentence or two about each if you can — Personal Injury, Family Law, etc.',
            }),
            'attorney_bios': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 5,
                'placeholder': 'Name, role, bar admissions, years of experience, education, notable cases. We\'ll format into proper bios.',
            }),
            'reference_sites': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': "3-5 sites you like the look of. For each one, tell us what you like and/or don't like — colors, layout, photos, feel.",
            }),
            'competitors': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': '3-5 firms you compete with most directly. Name + website if you know it.',
            }),
            'domain_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'johnsonlaw.com',
            }),
            'domain_registrar': forms.Select(attrs={
                'class': 'form-control',
                'data-registrar-select': '1',
            }),
            'domain_registrar_other': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Who is the domain registered with?',
                'data-registrar-other': '1',
            }),
            'facebook_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://facebook.com/your-firm',
            }),
            'instagram_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://instagram.com/your-firm',
            }),
            'linkedin_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://linkedin.com/company/your-firm',
            }),
            'twitter_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://x.com/your-firm',
            }),
            'google_business_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://maps.google.com/?cid=...',
            }),
            'social_links': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': 'Anything else — YouTube, TikTok, Avvo, Yelp, etc. One URL per line.',
            }),
        }
        labels = {
            'facebook_url': 'Facebook',
            'instagram_url': 'Instagram',
            'linkedin_url': 'LinkedIn',
            'twitter_url': 'X (Twitter)',
            'google_business_url': 'Google Business Profile',
            'social_links': 'Other social profiles',
            'domain_registrar_other': 'Registrar name',
        }


class RevisionForm(forms.ModelForm):
    """Client-submitted revision request."""

    class Meta:
        model = RevisionRequest
        fields = ['description', 'is_major']
        widgets = {
            'description': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': 'Describe the change you’d like — be as specific as you can.',
            }),
        }
        labels = {'is_major': 'Is this a major change?'}

    def clean_description(self):
        description = (self.cleaned_data.get('description') or '').strip()
        if len(description) < 20:
            raise forms.ValidationError(
                'Please describe the change in at least 20 characters.'
            )
        return description


class SupportTicketForm(forms.ModelForm):
    """Client-submitted support ticket."""

    class Meta:
        model = SupportTicket
        fields = ['subject', 'description', 'priority']
        widgets = {
            'subject': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
            'priority': forms.Select(attrs={'class': 'form-control'}),
        }


class FileUploadForm(forms.ModelForm):
    """Client file upload on the Files page."""

    class Meta:
        model = ClientDocument
        fields = ['file', 'label']
        widgets = {
            'file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'label': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'What is this file?',
            }),
        }

    def clean_file(self):
        uploaded = self.cleaned_data.get('file')
        if uploaded and uploaded.size > 50 * 1024 * 1024:
            raise forms.ValidationError('Files must be 50MB or smaller.')
        return uploaded


class SettingsForm(forms.ModelForm):
    """Client-editable account settings — covers contact preferences
    + the WHOIS-registrant info needed for domain registration."""

    class Meta:
        model = ClientProfile
        fields = [
            # Contact identity (required for WHOIS registrant)
            'contact_name', 'phone',
            'address', 'city', 'state', 'zip_code',
            # Preferences
            'preferred_contact_method', 'notify_on_stage_change',
        ]
        widgets = {
            'contact_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Jane Smith',
                'autocomplete': 'name',
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'tel',
                'placeholder': '(210) 555-1234',
                'inputmode': 'tel', 'autocomplete': 'tel',
                'maxlength': '14',
            }),
            'address': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '123 Main Street, Suite 200',
                'autocomplete': 'street-address',
            }),
            'city': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Austin',
                'autocomplete': 'address-level2',
            }),
            'state': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'TX',
                'autocomplete': 'address-level1',
                'maxlength': '50',
            }),
            'zip_code': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '78701',
                'inputmode': 'numeric', 'autocomplete': 'postal-code',
                'maxlength': '10',
            }),
            'preferred_contact_method': forms.Select(attrs={'class': 'form-control'}),
        }
        labels = {
            'contact_name': 'Your name',
            'phone': 'Phone',
            'address': 'Street address',
            'city': 'City',
            'state': 'State',
            'zip_code': 'ZIP code',
            'notify_on_stage_change': 'Email me when my project stage changes',
        }
        help_texts = {
            'contact_name': 'Used on invoices, contracts, and as the WHOIS registrant for any domains you register.',
            'address': 'Your business address. Required for domain registration (kept private by WHOIS privacy).',
        }

    def clean_phone(self):
        from core.phone_utils import normalize_phone
        return normalize_phone(self.cleaned_data.get('phone'))

    def clean_state(self):
        # 2-letter state code preferred but allow longer names.
        state = (self.cleaned_data.get('state') or '').strip()
        return state.upper() if len(state) == 2 else state

    def clean_zip_code(self):
        zip_code = (self.cleaned_data.get('zip_code') or '').strip()
        # Strip out any non-alphanumeric chars (allow ZIP+4 like
        # "78701-1234"). US-format check is best-effort.
        return zip_code
