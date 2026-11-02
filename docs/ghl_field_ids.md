# Refreshing `GHL_CUSTOM_FIELD_IDS`

`settings.GHL_CUSTOM_FIELD_IDS` pins a GHL custom-field **id** for every
key the website writes. Ids, not keys, because a key that matches no
field in the location is accepted by GHL and then ignored — the field
just never arrives, with nothing logged at the call site. That is how
`gclid` went missing for the whole life of the contact form (GHL's field
is `ad_click_id`).

Run this after creating or renaming any custom field in the GHL UI, and
paste the output over the dict in `settings.py`.

```bash
ssh root@161.35.108.209 'cd /var/www/aspired/app && sudo -u aspired \
  /var/www/aspired/venv/bin/python manage.py shell' <<'PY'
import requests
from django.conf import settings as s
r = requests.get(
    f'{s.GHL_API_BASE.rstrip("/")}/locations/{s.GHL_LOCATION_ID}/customFields',
    headers={'Authorization': f'Bearer {s.GHL_API_KEY}',
             'Version': s.GHL_API_VERSION, 'Accept': 'application/json'},
    timeout=25)
r.raise_for_status()
for f in sorted(r.json()['customFields'], key=lambda x: x['fieldKey']):
    short = f['fieldKey'].split('.', 1)[-1]
    print(f"    '{short}': '{f['id']}',")
PY
```

Then run the contract test, which fails if any key the code sends is
missing an id:

```bash
python manage.py test outreach.tests_ghl_field_ids
```

## Counting: the UI and the API disagree, and the API is right

The GHL UI showed **63** contact fields on 2026-11-01; this endpoint
returns **48**. Verified that day, before you go hunting for 15 lost
fields:

- The endpoint is **not paginated**. `limit`, `offset` and `page` are all
  rejected with `422 property should not exist`. One call is the whole set.
- `model=` accepts `contact | opportunity | all | business | task | ticket`.
  `contact` → 48, `all` → 48, `opportunity` → **0**, `business` → **0**.
- The 15-field gap is GHL's **standard** fields — First Name, Last Name,
  Email, Phone, Company Name, Address 1, City, State, Postal Code,
  Country, Website, Date of Birth, Timezone, Source, Tags. The UI lists
  them alongside custom fields (with padlock icons, no delete action);
  the API returns custom fields only. Confirmed by checking all 48
  returned keys against the standard-field names: zero overlap.
- Opportunity's 10 and Company's 12 UI entries are standard fields for
  the same reason — neither object has any custom fields.

Standard fields are **not** custom-field write targets. They go in the
top level of the upsert payload (`firstName`, `email`, `phone`,
`companyName`), which `outreach.ghl.build_payload` already does.

## Two keys are misspelled in GHL

`goggle_rating` (sic) and `_google_reviews` (leading underscore). Kept
verbatim. The id is what travels in the payload, so the misspelling is
cosmetic, and renaming them in the GHL UI would orphan the data already
on existing contacts.

## Known token scope gaps

The Private Integration Token can read `customFields` but **not**:

- `GET /locations/{id}/tags` → 401
- `GET /locations/{id}/customValues` → 401

So tag existence cannot be verified from here — check tags in the GHL
UI. `add_tags` writes via `POST /contacts/{id}/tags`, a *contacts*
scope, which is granted.
