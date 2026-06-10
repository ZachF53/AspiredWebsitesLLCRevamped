"""
Phase 5a — Composer form.
"""

from django import forms

from social.google_gbp import GBP_POST_MAX_CHARS


class ComposePostForm(forms.Form):
    """Operator-facing form for drafting a GBP post."""

    ai_topic = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. "Estate planning for blended families"',
            'autocomplete': 'off',
        }),
        label='AI topic (optional — Generate button uses this)',
    )
    body = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 8,
            'maxlength': str(GBP_POST_MAX_CHARS),
            'placeholder': 'Post body — up to 1,500 characters.',
        }),
        max_length=GBP_POST_MAX_CHARS,
        help_text=(
            f'Up to {GBP_POST_MAX_CHARS:,} characters. No emoji, no '
            f'hashtags — both get de-emphasised on Google Business '
            f'Profile.'),
    )
    media_url = forms.URLField(
        required=False,
        widget=forms.URLInput(attrs={
            'class': 'form-control',
            'placeholder': 'https://...',
        }),
        label='Image / video URL (optional)',
    )
    scheduled_for = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={
            'class': 'form-control',
            'type': 'datetime-local',
        }),
        label='Schedule for (leave blank to save as draft)',
    )
    save_as_draft = forms.BooleanField(
        required=False,
        label='Save as draft (do not schedule)',
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('save_as_draft') and not cleaned.get('scheduled_for'):
            raise forms.ValidationError(
                'Pick a Schedule time, or check Save as draft.')
        return cleaned
