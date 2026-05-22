"""Vault forms."""

from django import forms

from .models import ServerCommandLibrary, VaultCredential


class CredentialForm(forms.Form):
    """
    Add/edit a credential. Not a ModelForm — the model stores encrypted
    fields; the view handles encryption. On edit, the `change_*` flags say
    which sensitive web-login fields to re-encrypt. SSH fields are pre-filled
    (decrypted) on edit and re-encrypted wholesale on save.
    """

    label = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'placeholder': 'DigitalOcean Root Login',
        }),
    )
    category = forms.ChoiceField(
        choices=VaultCredential.CATEGORY_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    username = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
    )
    password = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
    )
    url = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'placeholder': 'https://...', 'autocomplete': 'off',
        }),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )
    visible_to_client = forms.BooleanField(required=False)
    sort_order = forms.IntegerField(
        required=False, initial=0,
        widget=forms.NumberInput(attrs={'class': 'form-control'}),
    )

    # Edit-only: only re-encrypt a sensitive web-login field when its flag is set.
    change_username = forms.BooleanField(required=False)
    change_password = forms.BooleanField(required=False)
    change_url = forms.BooleanField(required=False)
    change_notes = forms.BooleanField(required=False)

    # ── SSH fields ──
    is_ssh_credential = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'id': 'id_is_ssh_credential'}),
    )
    ssh_host = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'autocomplete': 'off',
            'placeholder': '161.35.108.209 or server.domain.com',
        }),
    )
    ssh_port = forms.IntegerField(
        required=False, initial=22,
        widget=forms.NumberInput(attrs={'class': 'form-control'}),
    )
    ssh_username = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control', 'autocomplete': 'off', 'placeholder': 'root',
        }),
    )
    ssh_auth_type = forms.ChoiceField(
        required=False, choices=VaultCredential.SSH_AUTH_CHOICES, initial='password',
        widget=forms.RadioSelect,
    )
    ssh_password = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
    )
    ssh_private_key = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control', 'rows': 6, 'autocomplete': 'off',
            'placeholder': '-----BEGIN OPENSSH PRIVATE KEY-----',
        }),
    )
    ssh_key_passphrase = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
    )

    def clean_sort_order(self):
        return self.cleaned_data.get('sort_order') or 0

    def clean_ssh_port(self):
        return self.cleaned_data.get('ssh_port') or 22

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('is_ssh_credential'):
            if not (cleaned.get('ssh_host') or '').strip():
                self.add_error('ssh_host', 'Server host is required for SSH.')
            if not (cleaned.get('ssh_username') or '').strip():
                self.add_error('ssh_username', 'Username is required for SSH.')
        return cleaned


class CommandForm(forms.ModelForm):
    """Add a saved command to an SSH credential's command library."""

    class Meta:
        model = ServerCommandLibrary
        fields = [
            'label', 'command', 'category',
            'requires_confirmation', 'is_dangerous', 'sort_order',
        ]
        widgets = {
            'label': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Check all services'}),
            'command': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'supervisorctl status'}),
            'category': forms.Select(attrs={'class': 'form-control'}),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
        }
