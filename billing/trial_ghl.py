"""
Pushing trial lifecycle events to GoHighLevel.

GHL owns the automation once a tag lands, so every function here ends
in a tag. That is the contract the rest of the business is built on:
Django decides what happened, a tag says so, and a GHL workflow does
the messaging. No workflow endpoint is ever called from Python.

## The rules this file obeys, and what breaks if it does not

**Never send a ``tags`` key on the upsert.** GHL replaces a contact's
ENTIRE tag array on ``/contacts/upsert``. A customer tagged
["client", "hvac", "onboarding-complete", "paid-hvac-voice-receptionist"]
who hits any upsert here would come back as just the one tag, which
destroys client status and re-fires intake automation on a paying
account. Tagging is always a second, additive call. This is documented
at length in outreach/ghl.py and is the single most expensive mistake
available in this integration.

**Custom field keys are bare.** ``trial_start``, not
``contact.trial_start``.

**Split the name on the first space.** GHL's
``{{contact.first_name}}`` merge field appears in automated messages,
and sending "Dave Moreno" whole renders "Hi Dave Moreno,".

**companyName is a top-level property**, not a custom field.

## The four custom fields may not exist yet

``trial_start``, ``trial_end``, ``stripe_customer_id`` and
``stripe_subscription_id`` are created by hand in GHL (they are on the
owner checklist). Until they are, GHL accepts the upsert and silently
drops the unknown keys. Nothing here creates them from code and nothing
here treats their absence as a failure: the TAG is what fires the
workflow, and a tag landing without its fields is a degraded success,
not an outage.

## Failure never costs a signup

Every entry point catches GHLError and records a FailedLeadSubmission,
the same safety net the public contact form uses. A GHL outage must not
turn into a 500 on a Stripe webhook, because Stripe would then retry
the whole event and re-run everything else in the handler.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


# Tag templates. Product slug is appended so one GHL workflow can key
# on the product, e.g. trial-started-hvac-voice-receptionist.
TRIAL_STARTED = 'trial-started'
TRIAL_ENDING = 'trial-ending'
PAID = 'paid'
CANCELLED = 'cancelled'
PAYMENT_FAILED = 'payment-failed'


def tag_for(kind, product_slug):
    """e.g. ('trial-started', 'hvac-review-system') -> the tag string."""
    return f'{kind}-{product_slug}'


def _record_failure(*, stage, product_slug, email, error, payload=None):
    """Log a failed GHL push so no trial signup is ever silently lost.

    Reuses FailedLeadSubmission, the table the public form path already
    writes to, rather than inventing a parallel one. `lead` is nullable,
    which is what makes it usable here: a trial buyer is a customer, not
    a Lead row.
    """
    try:
        from outreach.models import FailedLeadSubmission
        FailedLeadSubmission.objects.create(
            lead=None,
            payload={
                'source': 'billing.trial_ghl',
                'stage': stage,
                'product_slug': product_slug,
                'email': email,
                'attempted': payload or {},
            },
            error=str(error)[:4000],
        )
    except Exception:  # noqa: BLE001
        # If even the failure record fails, the log line is all we have.
        logger.exception(
            'trial_ghl: could not record failure for %s (%s)', email, stage)


def _upsert(*, email, name='', phone='', company_name='', custom_fields=None,
            source='trial-signup'):
    """Upsert the contact and return its id. Raises GHLError.

    No tags here. See module docstring.
    """
    from outreach.ghl import build_payload, push_contact, split_name

    first_name, last_name = split_name(name)
    if not first_name:
        # GHL requires something; the local part of the email is a
        # better placeholder than an empty string, which renders
        # "Hi ," in the welcome message.
        first_name = (email or '').split('@', 1)[0] or 'there'

    payload = build_payload(
        first_name=first_name, last_name=last_name, email=email,
        phone_raw=phone, company_name=company_name,
        custom_fields=custom_fields or {}, source=source,
    )
    body = push_contact(payload)
    contact_id = (body.get('contact') or {}).get('id', '')
    if not contact_id:
        from outreach.ghl import GHLError
        raise GHLError('GHL upsert returned no contact id')
    return contact_id, payload


def _iso_date(value):
    """A date string GHL's date fields accept, or '' if unknown.

    Date-typed custom fields in GHL take ISO 8601. Sending a unix
    timestamp lands a meaningless integer in a date field, which then
    renders as a wrong date in the workflow email rather than failing
    loudly.
    """
    if not value:
        return ''
    try:
        return value.date().isoformat()
    except AttributeError:
        return ''


def push_trial_started(*, email, product_slug, name='', phone='',
                       company_name='', trial_start=None, trial_end=None,
                       customer_id='', subscription_id=''):
    """Upsert the contact, set the trial fields, then tag trial-started.

    Returns the GHL contact id, or '' if the push failed (already
    recorded). Never raises: the caller is a Stripe webhook.
    """
    from outreach.ghl import GHLError, add_tags

    custom_fields = {
        'trial_start': _iso_date(trial_start or timezone.now()),
        'trial_end': _iso_date(trial_end),
        'stripe_customer_id': customer_id or '',
        'stripe_subscription_id': subscription_id or '',
    }
    payload = None
    try:
        contact_id, payload = _upsert(
            email=email, name=name, phone=phone, company_name=company_name,
            custom_fields=custom_fields,
            source=f'trial-signup-{product_slug}')
    except GHLError as exc:
        _record_failure(stage='upsert', product_slug=product_slug,
                        email=email, error=exc,
                        payload=getattr(exc, 'payload', None))
        return ''
    except Exception as exc:  # noqa: BLE001
        _record_failure(stage='upsert-unexpected', product_slug=product_slug,
                        email=email, error=exc)
        return ''

    tag = tag_for(TRIAL_STARTED, product_slug)
    try:
        add_tags(contact_id, [tag])
    except Exception as exc:  # noqa: BLE001
        # The contact exists but the workflow did not fire, which is the
        # half-done state worth a human's attention: the customer is on
        # a trial and will receive none of the onboarding sequence.
        _record_failure(stage='tag', product_slug=product_slug, email=email,
                        error=exc,
                        payload={'contact_id': contact_id, 'tags': [tag],
                                 'upsert': payload})
        return contact_id

    logger.info('trial_ghl: tagged %s on contact %s', tag, contact_id)
    return contact_id


def _tag_existing(*, email, product_slug, add=(), remove=(), stage):
    """Find-or-create the contact by email, then add/remove tags.

    An upsert is used to resolve the contact id rather than a search,
    because by this point the contact reliably exists (trial-started
    created it) and upsert-by-email is the one lookup this integration
    is known-good at. It carries no tags, so it cannot clobber the
    array.
    """
    from outreach.ghl import GHLError, add_tags, remove_tags

    try:
        contact_id, payload = _upsert(
            email=email, source=f'trial-{stage}-{product_slug}')
    except (GHLError, Exception) as exc:  # noqa: BLE001
        _record_failure(stage=f'{stage}-upsert', product_slug=product_slug,
                        email=email, error=exc,
                        payload=getattr(exc, 'payload', None))
        return ''

    try:
        if add:
            add_tags(contact_id, list(add))
        if remove:
            # Removal is best-effort relative to the add: a paying
            # customer who keeps a stale trial-ending tag is a
            # cosmetic problem, where a paying customer with no paid
            # tag is a billing-state problem. So the add goes first.
            remove_tags(contact_id, list(remove))
    except Exception as exc:  # noqa: BLE001
        _record_failure(stage=f'{stage}-tag', product_slug=product_slug,
                        email=email, error=exc,
                        payload={'contact_id': contact_id,
                                 'add': list(add), 'remove': list(remove),
                                 'upsert': payload})
        return contact_id

    logger.info('trial_ghl: %s add=%s remove=%s on contact %s',
                stage, list(add), list(remove), contact_id)
    return contact_id


def push_trial_ending(*, email, product_slug):
    """Stripe fires trial_will_end 3 days before the first charge."""
    return _tag_existing(
        email=email, product_slug=product_slug,
        add=[tag_for(TRIAL_ENDING, product_slug)], stage='trial-ending')


def push_converted(*, email, product_slug):
    """First paid invoice: tag paid, clear this product's trial tags.

    Both trial tags are removed, not just trial-ending. A converted
    customer still carrying trial-started would keep matching any GHL
    workflow filtered on "is on a trial", which is how a paying client
    ends up in a nurture sequence telling them to start their trial.
    """
    return _tag_existing(
        email=email, product_slug=product_slug,
        add=[tag_for(PAID, product_slug)],
        remove=[tag_for(TRIAL_STARTED, product_slug),
                tag_for(TRIAL_ENDING, product_slug)],
        stage='paid')


def push_cancelled(*, email, product_slug):
    return _tag_existing(
        email=email, product_slug=product_slug,
        add=[tag_for(CANCELLED, product_slug)], stage='cancelled')


def push_payment_failed(*, email, product_slug):
    return _tag_existing(
        email=email, product_slug=product_slug,
        add=[tag_for(PAYMENT_FAILED, product_slug)],
        stage='payment-failed')
