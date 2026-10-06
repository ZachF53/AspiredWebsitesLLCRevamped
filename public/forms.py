from django import forms

from outreach.models import Lead


class ContactForm(forms.Form):
    """
    Public-facing contact form. Saves to a Lead row with source='contact_form'
    per CLAUDE.md → Data Model Decisions → Contact Form → Lead Mapping, and
    pushes the same submission to GoHighLevel (outreach/ghl.py) per the
    2026-10-05 GHL integration spec.

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

    2026-10-05 — business_name/project_type/budget_range/timeline/
    current_website/sms_consent added for the GHL integration. These
    describe the WEBSITE PROJECT (what, budget, how soon, existing
    site) — a different axis from the Sept trade/trucks/software
    qualifiers below, which describe the BUSINESS calling in. Both
    sets are optional and coexist; none of them replace each other.
    business_name reintroduces what firm_name was for pre-pivot, so it
    maps back to Lead.firm_name.
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

    business_name = forms.CharField(
        label='Business Name', max_length=255, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Doe Heating & Air',
            'autocomplete': 'organization',
        }),
    )

    message = forms.CharField(
        label='Message',
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'placeholder': 'Tell us about your business and what you need.',
            'rows': 5,
        }),
    )

    # Optional qualifiers (re-audit 2026-09-26, plan M-4.06): which trade,
    # how big, and what job software they run, so the call starts with
    # the answers. All optional; blank keeps the old behaviour.
    TRADE_CHOICES = [
        ('', 'Choose one (optional)'),
        ('HVAC', 'HVAC'),
        ('Plumbing', 'Plumbing'),
        ('Electrical', 'Electrical'),
        ('Other home service', 'Other home service'),
    ]
    TRUCK_CHOICES = [
        ('', 'Choose one (optional)'),
        ('Not started yet', 'Just starting out'),
        ('1', '1'),
        ('2-5', '2 to 5'),
        ('6-15', '6 to 15'),
        ('16+', '16 or more'),
    ]
    trade = forms.ChoiceField(
        label='Trade', choices=TRADE_CHOICES, required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    trucks = forms.ChoiceField(
        label='Trucks on the road', choices=TRUCK_CHOICES, required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    software = forms.CharField(
        label='Job or scheduling software, if any', max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. ServiceTitan, Housecall Pro, Jobber, none',
        }),
    )

    # ── Website-project qualifiers (2026-10-05 GHL integration) ────────
    # Describe the project itself, not the business — see class
    # docstring. All optional; sent to GHL as custom fields and folded
    # into inquiry_text same as trade/trucks/software above.
    PROJECT_TYPE_CHOICES = [
        ('', 'Choose one (optional)'),
        ('New website', 'New website'),
        ('Redesign existing site', 'Redesign existing site'),
        ('Not sure yet', 'Not sure yet'),
    ]
    BUDGET_RANGE_CHOICES = [
        ('', 'Choose one (optional)'),
        ('Under $2,000', 'Under $2,000'),
        ('2000-5000', '$2,000 - $5,000'),
        ('5000+', '$5,000+'),
        ('Not sure yet', 'Not sure yet'),
    ]
    TIMELINE_CHOICES = [
        ('', 'Choose one (optional)'),
        ('ASAP', 'ASAP'),
        ('1-3 months', '1-3 months'),
        ('3+ months', '3+ months'),
        ('Just looking', 'Just looking'),
    ]
    project_type = forms.ChoiceField(
        label='Project type', choices=PROJECT_TYPE_CHOICES, required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    budget_range = forms.ChoiceField(
        label='Budget range', choices=BUDGET_RANGE_CHOICES, required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    timeline = forms.ChoiceField(
        label='Timeline', choices=TIMELINE_CHOICES, required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    current_website = forms.CharField(
        label='Current website, if any', max_length=500, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'type': 'url',
            'placeholder': 'https://your-current-site.com',
        }),
    )

    # SMS consent — carrier-reviewed, must stay unchecked by default and
    # NOT required. One checkbox per purpose (this one covers project/
    # transactional texts only) — see claude-code-website-spec.md Job 3.
    sms_consent = forms.BooleanField(required=False)

    # UTM / attribution — populated by JS (core/static/js/utm_capture.js),
    # never typed by the user. Plain CharFields, no validation: a JS
    # failure must degrade to an empty string, never a broken form.
    utm_source = forms.CharField(max_length=500, required=False)
    utm_medium = forms.CharField(max_length=500, required=False)
    utm_campaign = forms.CharField(max_length=500, required=False)
    utm_term = forms.CharField(max_length=500, required=False)
    utm_content = forms.CharField(max_length=500, required=False)
    gclid = forms.CharField(max_length=500, required=False)
    fbclid = forms.CharField(max_length=500, required=False)
    landing_page = forms.CharField(max_length=500, required=False)
    referrer = forms.CharField(max_length=500, required=False)

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
        qualifiers = [
            f'{label}: {value}' for label, value in (
                ('Trade', cleaned.get('trade')),
                ('Trucks', cleaned.get('trucks')),
                ('Software', (cleaned.get('software') or '').strip()),
                ('Project type', cleaned.get('project_type')),
                ('Budget range', cleaned.get('budget_range')),
                ('Timeline', cleaned.get('timeline')),
                ('Current website',
                 (cleaned.get('current_website') or '').strip()),
            ) if value
        ]
        inquiry = cleaned['message']
        if qualifiers:
            inquiry = inquiry + '\n\n' + '\n'.join(qualifiers)
        return Lead.objects.create(
            firm_name=(cleaned.get('business_name') or '').strip(),
            attorney_name=cleaned['name'],
            business_type=cleaned.get('trade') or 'HVAC',
            phone=cleaned['phone'],
            email=cleaned['email'],
            inquiry_text=inquiry,
            source='contact_form',
            status='new',
            score=0,
            ip_address=ip_address,
            referral_code=(referral_code or '').upper()[:20],
        )


class CallbackForm(forms.Form):
    """
    "Call me back" (plan M-4.05): the shortest possible path for a phone
    user who won't fill in the long form or wait for the calendar to load.
    Name and phone only, plus an optional best time. Saved as a Lead
    (source='contact_form', tagged 'callback') and emailed to the owner;
    no SMS alert by owner decision (2026-09-25).
    """

    name = forms.CharField(
        label='Name', max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'autocomplete': 'name',
        }),
    )
    phone = forms.CharField(
        label='Phone', max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'type': 'tel', 'autocomplete': 'tel',
            'inputmode': 'tel', 'maxlength': '14',
        }),
    )
    # Four slots a human (or the GHL workflow) can actually act on,
    # rather than free text nobody can route on.
    #
    # Values are BYTE-EXACT to GHL's "best_time" SINGLE_OPTIONS custom
    # field (verified live against the GHL API 2026-10-06) — GHL
    # silently drops a dropdown value that doesn't match an existing
    # option (HTTP 200, nothing written, no error anywhere). That
    # means: plain ASCII hyphen in the ranges (NOT an en dash — looks
    # identical in a browser, parses differently, and would silently
    # drop), lowercase am/pm with no periods or leading space, and
    # value == label exactly. Do not "clean up" this formatting.
    #
    # Blank is allowed and stays blank (not mapped to a value) — the
    # GHL workflow treats blank as "call now". BEST_TIME_LABELS below
    # maps blank to a friendly label for Django-side display only
    # (Lead.inquiry_text, the internal notification email); the blank
    # string itself is still what gets sent to GHL.
    BEST_TIME_CHOICES = [
        ('', 'Choose one (optional)'),
        ('As soon as possible', 'As soon as possible'),
        ('Morning (8am-12pm)', 'Morning (8am-12pm)'),
        ('Afternoon (12pm-5pm)', 'Afternoon (12pm-5pm)'),
        ('Evening (5pm-8pm)', 'Evening (5pm-8pm)'),
    ]
    BEST_TIME_LABELS = dict(BEST_TIME_CHOICES)
    BEST_TIME_LABELS[''] = 'As soon as possible'
    best_time = forms.ChoiceField(
        label='Best time', choices=BEST_TIME_CHOICES, required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )

    # UTM/attribution — same deal as ContactForm's: populated by
    # core/static/js/utm_capture.js, never typed by the user, and a JS
    # failure must degrade to an empty string, not a broken form. The
    # callback form only needs landing_page/referrer per the GHL spec
    # (it has no ad-campaign custom fields to attribute to).
    landing_page = forms.CharField(max_length=500, required=False)
    referrer = forms.CharField(max_length=500, required=False)

    website_url = forms.CharField(required=False)
    form_timestamp = forms.CharField(required=False)

    def clean_phone(self):
        from core.phone_utils import normalize_phone
        phone = normalize_phone(self.cleaned_data.get('phone'))
        digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a phone number we can call.')
        return phone

    def save_as_lead(self, ip_address=None):
        cleaned = self.cleaned_data
        best_label = self.BEST_TIME_LABELS.get(
            cleaned.get('best_time', ''), 'As soon as possible')
        return Lead.objects.create(
            firm_name='',
            attorney_name=cleaned['name'],
            business_type='HVAC',
            phone=cleaned['phone'],
            email='',
            inquiry_text=f'Callback requested. Best time: {best_label}',
            source='contact_form',
            tags='callback',
            status='new',
            score=0,
            ip_address=ip_address,
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
            'invalid': 'Please enter a valid URL, e.g. https://yourbusiness.com',
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
