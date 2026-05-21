"""Admin-only forms for manual lead entry + inline updates."""

from django import forms

from outreach.models import Lead, LeadNote
from outreach.scraper import PRACTICE_AREAS


class ScrapeForm(forms.Form):
    """Triggers a lead-scraping run from the admin dashboard."""

    SOURCE_CHOICES = [
        ('google_maps', 'Google Maps'),
        ('texas_bar', 'Texas State Bar'),
        ('georgia_bar', 'Georgia State Bar'),
    ]
    STATE_CHOICES = [
        ('TX', 'Texas'),
        ('GA', 'Georgia'),
    ]
    MAX_RESULTS_CHOICES = [
        ('10', '10 results'),
        ('20', '20 results'),
        ('50', '50 results'),
    ]

    source = forms.ChoiceField(
        label='Source',
        choices=SOURCE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    practice_area = forms.ChoiceField(
        label='Practice Area / Niche',
        choices=[(p, p) for p in PRACTICE_AREAS],
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    city = forms.CharField(
        label='City',
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'San Antonio',
        }),
    )
    state = forms.ChoiceField(
        label='State',
        choices=STATE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
        help_text='Used for Google Maps. State Bar sources set this automatically.',
    )
    max_results = forms.ChoiceField(
        label='Max Results',
        choices=MAX_RESULTS_CHOICES,
        initial='20',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )


class LeadAddForm(forms.ModelForm):
    """
    Manual lead entry. Subset of Lead fields a human would actually know
    when typing in a new prospect. Score + temperature are auto-calculated
    on save. CRM-state fields (sequence_*, unsubscribed*, etc.) are not
    exposed here — they're set by the outreach pipeline.
    """

    class Meta:
        model = Lead
        fields = [
            'firm_name', 'attorney_name', 'practice_area', 'business_type',
            'email', 'phone', 'website',
            'city', 'state', 'address',
            'status', 'tags', 'notes',
        ]
        widgets = {
            'firm_name':       forms.TextInput(attrs={'class': 'form-control', 'autofocus': True}),
            'attorney_name':   forms.TextInput(attrs={'class': 'form-control'}),
            'practice_area':   forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Family Law'}),
            'business_type':   forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Law Firm, Contractor, Restaurant…'}),
            'email':           forms.EmailInput(attrs={'class': 'form-control'}),
            'phone':           forms.TextInput(attrs={'class': 'form-control', 'placeholder': '(210) 555-1234'}),
            'website':         forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com'}),
            'city':            forms.TextInput(attrs={'class': 'form-control'}),
            'state':           forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Texas / Georgia'}),
            'address':         forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'status':          forms.Select(attrs={'class': 'form-control'}),
            'tags':            forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'comma-separated'}),
            'notes':           forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Internal CRM notes (not visible to lead)…'}),
        }

    def clean_firm_name(self):
        return (self.cleaned_data.get('firm_name') or '').strip()


class LeadNoteForm(forms.ModelForm):
    """Tiny single-field form for the HTMX add-note flow on lead detail."""

    class Meta:
        model = LeadNote
        fields = ['note']
        widgets = {
            'note': forms.Textarea(attrs={
                'id': 'note-input',
                'name': 'note',
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Add an internal note — what was said, what to follow up on…',
                'required': True,
            }),
        }
