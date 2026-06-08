"""
Schedule-a-Call models.

AvailabilityWindow — admin-defined "you can book me between these hours"
ScheduledCall    — a confirmed (or held) booking
GoogleCalendarToken — OAuth refresh token for the admin's connected
                       Google Calendar (one per admin).
"""

import uuid

from django.conf import settings
from django.db import models


DAY_CHOICES = [
    (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'),
    (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
]


class AvailabilityWindow(models.Model):
    """One bookable window. Defaults (per spec):
        Mon-Fri 4pm-8pm ET, Sat 9am-8pm ET, Sun closed.
    """

    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    timezone = models.CharField(max_length=50, default='America/New_York')
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['day_of_week', 'start_time']

    def __str__(self):
        return (f'{self.get_day_of_week_display()} '
                f'{self.start_time:%H:%M}–{self.end_time:%H:%M} {self.timezone}')


class ScheduledCall(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Lead created from the contact form on /design/schedule/ — nullable
    # because the call may be scheduled before the form is finished.
    lead = models.ForeignKey(
        'outreach.Lead', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='scheduled_calls',
    )
    # When this slot becomes confirmed, we push it as an event to the
    # admin's Google Calendar and remember the event id so cancels
    # can clean up.
    google_event_id = models.CharField(max_length=120, blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=[
        ('held', 'Held — waiting on form completion'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
    ], default='held')
    customer_name = models.CharField(max_length=200, blank=True)
    customer_email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When a `held` slot becomes invalid (default 15 min).',
    )

    class Meta:
        ordering = ['starts_at']

    def __str__(self):
        return f'Call {self.customer_name or "(anon)"} @ {self.starts_at}'


class GoogleCalendarToken(models.Model):
    """One row per connected admin Google account."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='google_calendar_token',
    )
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()
    calendar_id = models.CharField(
        max_length=200, default='primary',
        help_text='Which calendar to read/write — primary by default.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Google Calendar token — {self.user}'
