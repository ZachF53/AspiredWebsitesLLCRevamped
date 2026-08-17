"""Forms for the client portal."""

from django import forms

from .account_models import Account
from .models import (
    ClientDocument,
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
            'gmb_status',
            'facebook_url', 'instagram_url', 'linkedin_url',
            'twitter_url', 'google_business_url', 'social_links',
        ]
        widgets = {
            'gmb_status': forms.RadioSelect(),
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
                'placeholder': 'One per line. A sentence or two about each if you can — e.g. Consulting, Installations, Catering, etc.',
            }),
            'attorney_bios': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 5,
                'placeholder': 'Name, role, years of experience, background, notable achievements. We\'ll format into proper bios.',
            }),
            'reference_sites': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': "3-5 sites you like the look of. For each one, tell us what you like and/or don't like — colors, layout, photos, feel.",
            }),
            'competitors': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': '3-5 businesses you compete with most directly. Name + website if you know it.',
            }),
            'domain_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'yourbusiness.com',
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
                'class': 'form-control', 'placeholder': 'https://facebook.com/yourbusiness',
            }),
            'instagram_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://instagram.com/yourbusiness',
            }),
            'linkedin_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://linkedin.com/company/yourbusiness',
            }),
            'twitter_url': forms.URLInput(attrs={
                'class': 'form-control', 'placeholder': 'https://x.com/yourbusiness',
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

    # Phase 7.5 — explicit allow-list of file types accepted by the
    # portal. Anything outside this set is rejected at form-clean. We
    # deliberately exclude executables, archives, and code (PHP/etc.)
    # to limit attack surface from a compromised client account.
    ALLOWED_EXTS = {
        # docs
        'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md',
        'xls', 'xlsx', 'ods', 'csv', 'ppt', 'pptx',
        # images
        'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'heic',
        # media (briefs / training)
        'mp4', 'mov', 'webm', 'mp3', 'wav', 'm4a',
        # design assets clients send
        'psd', 'ai', 'sketch', 'fig',
    }
    ALLOWED_MIME_PREFIXES = (
        'application/pdf', 'application/msword',
        'application/vnd.openxmlformats-officedocument',
        'application/vnd.oasis.opendocument',
        'application/rtf', 'application/vnd.ms-excel',
        'application/vnd.ms-powerpoint',
        'text/plain', 'text/csv', 'text/markdown',
        'image/', 'video/', 'audio/',
    )

    def clean_file(self):
        import os
        uploaded = self.cleaned_data.get('file')
        if not uploaded:
            return uploaded
        # Size: 50MB max
        if uploaded.size > 50 * 1024 * 1024:
            raise forms.ValidationError('Files must be 50MB or smaller.')
        # Extension: server-side allow-list. Client-side accept is hint
        # only — a crafted POST could bypass it.
        ext = os.path.splitext(uploaded.name)[1].lower().lstrip('.')
        if ext not in self.ALLOWED_EXTS:
            raise forms.ValidationError(
                f'File type ".{ext}" is not allowed. Send your file in '
                f'a standard document, image, or media format.')
        # Content-type sanity check — defense in depth against renamed
        # executables. Browsers populate content_type from the file's
        # MIME; allowed prefixes cover the formats above.
        ct = (uploaded.content_type or '').lower()
        if ct and not any(ct.startswith(p)
                          for p in self.ALLOWED_MIME_PREFIXES):
            raise forms.ValidationError(
                f'File content type "{ct}" is not allowed.')
        return uploaded


class SettingsForm(forms.ModelForm):
    """Client-editable account settings — covers contact preferences
    + the WHOIS-registrant info needed for domain registration."""

    class Meta:
        model = Account
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
