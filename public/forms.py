from django import forms

from outreach.models import Lead


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


class ContactForm(forms.ModelForm):
    """Public-facing contact form. Persists to Lead in outreach app."""

    class Meta:
        model = Lead
        fields = [
            'name',
            'business_name',
            'business_type',
            'phone',
            'email',
            'source',
            'message',
        ]
        labels = {
            'name': 'Full Name',
            'business_name': 'Business Name',
            'business_type': 'Business Type',
            'phone': 'Phone',
            'email': 'Email',
            'source': 'How did you hear about us?',
            'message': 'Message',
        }
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Jane Smith',
                'autocomplete': 'name',
                'maxlength': 120,
            }),
            'business_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Smith & Co.',
                'autocomplete': 'organization',
                'maxlength': 200,
            }),
            'business_type': forms.Select(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'tel',
                'placeholder': '(210) 555-1234',
                'autocomplete': 'tel',
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'jane@business.com',
                'autocomplete': 'email',
            }),
            'source': forms.Select(attrs={'class': 'form-control'}),
            'message': forms.Textarea(attrs={
                'class': 'form-control',
                'placeholder': 'Tell us about your business and what you need.',
                'rows': 5,
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Prepend placeholder option to required choice fields so the user
        # must explicitly pick something instead of accepting the first item.
        self.fields['business_type'].choices = (
            [('', '— Select business type —')]
            + [c for c in Lead.BUSINESS_TYPE_CHOICES]
        )
        self.fields['source'].choices = (
            [('', '— Optional —')]
            + [c for c in Lead.SOURCE_CHOICES]
        )
        self.fields['source'].required = False
