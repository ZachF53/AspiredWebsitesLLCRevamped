"""Admin-only forms for manual lead entry + inline updates."""

from django import forms

from billing.pricing_models import ServiceTier
from clients.models import ClientProfile, SiteChangelogEntry
from outreach.models import Lead, LeadNote
from outreach.scraper import PRACTICE_AREAS
from reporting.models import ClientChatbot, TrackedKeyword

from .models import DeploymentLog


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
            'email':           forms.EmailInput(attrs={
                'class': 'form-control',
                'autocapitalize': 'none', 'autocorrect': 'off',
                'spellcheck': 'false', 'inputmode': 'email',
            }),
            'phone':           forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'tel',
                'placeholder': '(210) 555-1234',
                'inputmode': 'tel', 'autocomplete': 'tel',
                'maxlength': '14',
            }),
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

    def clean_phone(self):
        from core.phone_utils import normalize_phone
        return normalize_phone(self.cleaned_data.get('phone'))

    def clean_email(self):
        return (self.cleaned_data.get('email') or '').strip().lower()


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


class ServiceTierForm(forms.ModelForm):
    """Edit a pricing tier from the admin-dashboard pricing manager."""

    class Meta:
        model = ServiceTier
        fields = [
            'name', 'tagline', 'description', 'price', 'price_display',
            'stripe_price_id', 'stripe_product_id', 'is_active', 'is_featured',
            'sort_order', 'pages_included', 'practice_areas_included',
            'timeline_weeks',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'tagline': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Optional — e.g. "Most Popular"',
            }),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'price_display': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Leave blank to auto-generate',
            }),
            'stripe_price_id': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'price_...',
            }),
            'stripe_product_id': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'prod_...',
            }),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
            'pages_included': forms.NumberInput(attrs={'class': 'form-control'}),
            'practice_areas_included': forms.NumberInput(attrs={'class': 'form-control'}),
            'timeline_weeks': forms.NumberInput(attrs={'class': 'form-control'}),
        }


class DeploymentLogForm(forms.ModelForm):
    """Manually record a deployment on the deploy-history page."""

    class Meta:
        model = DeploymentLog
        fields = ['deploy_type', 'domain', 'server_ip', 'client', 'success', 'notes']
        widgets = {
            'deploy_type': forms.Select(attrs={'class': 'form-control'}),
            'domain': forms.TextInput(attrs={'class': 'form-control'}),
            'server_ip': forms.TextInput(attrs={'class': 'form-control'}),
            'client': forms.Select(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].required = False
        self.fields['client'].empty_label = '— None —'


class SiteChangelogForm(forms.ModelForm):
    """Add / edit a single client site changelog entry."""

    class Meta:
        model = SiteChangelogEntry
        fields = [
            'client', 'date_of_change', 'change_type', 'title',
            'description', 'url_changed', 'is_client_visible',
        ]
        widgets = {
            'client': forms.Select(attrs={'class': 'form-control'}),
            'date_of_change': forms.DateInput(
                attrs={'class': 'form-control', 'type': 'date'},
                format='%Y-%m-%d',
            ),
            'change_type': forms.Select(attrs={'class': 'form-control'}),
            'title': forms.TextInput(attrs={
                'class': 'form-control', 'maxlength': 200,
                'placeholder': 'Updated practice area pages',
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': 'Added estate planning and probate pages based '
                               'on client intake. Updated meta descriptions on '
                               'all 8 practice area pages.',
            }),
            'url_changed': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://clientdomain.com/practice-areas/',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].empty_label = '— Select a client —'


class BlogGenerateForm(forms.Form):
    """Inputs for the AI blog post generator."""

    LENGTH_CHOICES = [
        ('short', 'Short (~500 words)'),
        ('medium', 'Medium (~800 words)'),
        ('long', 'Long (~1200 words)'),
    ]
    TONE_CHOICES = [
        ('professional', 'Professional'),
        ('conversational', 'Conversational'),
        ('authoritative', 'Authoritative'),
    ]

    client = forms.ModelChoiceField(
        queryset=ClientProfile.objects.order_by('firm_name'),
        empty_label='— Select a client —',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    topic = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'What to do after a car accident in Texas',
        }),
    )
    target_keyword = forms.CharField(
        max_length=200, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'personal injury lawyer San Antonio',
        }),
    )
    length = forms.ChoiceField(
        choices=LENGTH_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    tone = forms.ChoiceField(
        choices=TONE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )


class ChatbotConfigForm(forms.ModelForm):
    """Per-client chatbot configuration."""

    class Meta:
        model = ClientChatbot
        fields = [
            'is_active', 'greeting_message', 'faq_text', 'system_prompt',
            'primary_color', 'position',
        ]
        widgets = {
            'greeting_message': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2}),
            'system_prompt': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 5}),
            'faq_text': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 6,
                'placeholder': 'Office hours, common questions and answers, '
                               'service details — anything the bot should know.'}),
            'primary_color': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '#E8650A'}),
            'position': forms.Select(attrs={'class': 'form-control'}),
        }


class KeywordForm(forms.ModelForm):
    """Add a tracked keyword for a client (client is set by the view)."""

    class Meta:
        model = TrackedKeyword
        fields = ['keyword', 'target_url', 'notes']
        widgets = {
            'keyword': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'estate planning attorney san antonio',
            }),
            'target_url': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://clientdomain.com/estate-planning/',
            }),
            'notes': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Optional note',
            }),
        }


class ClientProfileEditForm(forms.ModelForm):
    """
    Full client profile edit form for /admin-dashboard/clients/<id>/edit/.

    Four sections in the rendered template — Basic Info, Website &
    Server, Flags, Internal Notes.

    `live_url` isn't on ClientProfile (it lives on Project); the view
    seeds the initial value from the linked project and writes back to
    it on save. The user's email is shown read-only in the template
    (lives on User; renaming from this form would be a footgun).
    """

    live_url = forms.URLField(
        required=False, label='Live URL',
        widget=forms.URLInput(attrs={
            'class': 'form-control',
            'placeholder': 'https://clientdomain.com',
        }),
        help_text=(
            'Used for uptime monitoring, SSL scans, and Nikto web '
            'scans. Required for full vulnerability scans.'),
    )
    # `package` is a CharField on the model with PACKAGE_CHOICES; per
    # the spec this section should use a plain text input here.
    package = forms.CharField(
        required=False, label='Package',
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    class Meta:
        model = ClientProfile
        fields = [
            # Section 1 — Basic info
            'firm_name', 'contact_name', 'business_type', 'status',
            'package', 'city', 'state', 'phone',
            # Section 2 — Website / server
            'do_droplet_ip', 'do_droplet_created_at',
            # Section 3 — Flags
            'maintenance_active', 'auto_send_scan_reports',
            'onboarding_complete', 'is_tester',
            # Section 4 — Internal notes
            'internal_notes',
        ]
        widgets = {
            'firm_name':       forms.TextInput(attrs={'class': 'form-control'}),
            'contact_name':    forms.TextInput(attrs={'class': 'form-control'}),
            'business_type':   forms.TextInput(attrs={'class': 'form-control'}),
            'status':          forms.Select(attrs={'class': 'form-control'}),
            'city':            forms.TextInput(attrs={'class': 'form-control'}),
            'state':           forms.TextInput(attrs={'class': 'form-control'}),
            'phone':           forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'tel',
                'placeholder': '(210) 555-1234',
                'inputmode': 'tel', 'autocomplete': 'tel',
                'maxlength': '14',
            }),
            'do_droplet_ip':   forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '161.35.108.209',
            }),
            'do_droplet_created_at': forms.DateInput(attrs={
                'class': 'form-control', 'type': 'date',
            }),
            'internal_notes': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 6,
            }),
        }

    def clean_phone(self):
        from core.phone_utils import normalize_phone
        return normalize_phone(self.cleaned_data.get('phone'))


# Per-field quick-edit on the client detail page. The keys here are the
# only ones the inline endpoint will accept — everything else 400s.
CLIENT_QUICK_EDIT_FIELDS = {
    'live_url':     {'type': 'url',   'label': 'Live URL'},
    'do_droplet_ip': {'type': 'text', 'label': 'Droplet IP'},
    'contact_name': {'type': 'text',  'label': 'Contact name'},
    'phone':        {'type': 'text',  'label': 'Phone'},
}
