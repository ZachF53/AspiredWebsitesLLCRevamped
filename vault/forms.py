"""Vault forms."""

from django import forms

from .models import VaultCredential


class CredentialForm(forms.Form):
    """
    Add/edit a credential. Not a ModelForm — the model stores encrypted
    fields; the view handles encryption. On edit, the `change_*` flags say
    which sensitive fields to re-encrypt (others keep their stored value).
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

    # Edit-only: only re-encrypt a sensitive field when its flag is set.
    change_username = forms.BooleanField(required=False)
    change_password = forms.BooleanField(required=False)
    change_url = forms.BooleanField(required=False)
    change_notes = forms.BooleanField(required=False)

    def clean_sort_order(self):
        return self.cleaned_data.get('sort_order') or 0
