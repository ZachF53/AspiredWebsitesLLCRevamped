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
    can be saved partially as the client works through the steps."""

    class Meta:
        model = IntakeResponse
        fields = [
            'brand_colors', 'brand_fonts', 'logo',
            'photos_provided', 'photos_note',
            'about_copy', 'practice_areas', 'attorney_bios',
            'reference_sites', 'competitors',
            'domain_name', 'domain_registrar',
            'google_business_access', 'social_links',
        ]
        widgets = {
            'brand_colors': forms.TextInput(attrs={'class': 'form-control'}),
            'brand_fonts': forms.TextInput(attrs={'class': 'form-control'}),
            'logo': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'photos_note': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'about_copy': forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
            'practice_areas': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'attorney_bios': forms.Textarea(attrs={'class': 'form-control', 'rows': 5}),
            'reference_sites': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'competitors': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'domain_name': forms.TextInput(attrs={'class': 'form-control'}),
            'domain_registrar': forms.Select(attrs={'class': 'form-control'}),
            'social_links': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
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
    """Client-editable account settings."""

    class Meta:
        model = ClientProfile
        fields = ['phone', 'preferred_contact_method', 'notify_on_stage_change']
        widgets = {
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'preferred_contact_method': forms.Select(attrs={'class': 'form-control'}),
        }
        labels = {
            'notify_on_stage_change': 'Email me when my project stage changes',
        }
