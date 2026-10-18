---
name: social-media-upload
description: Upload a batch of rendered social images to the GoHighLevel Media Library and fill the image URLs into that batch's Social Planner CSVs. Use when given a folder of *-portrait.png social posts to publish, or when asked to upload social images to GHL or to fix placeholder <<MEDIA_URL:...>> tokens in a social CSV.
---

# Social batch upload to GoHighLevel

## Run it

```bash
python3 .claude/skills/social-media-upload/scripts/ghl_social_upload.py "<folder>"
```

The unzipped batch folder path is the **only** input. Nothing else to find,
pass, or set up. Add `--dry-run` first to see the plan without changing
anything. Relay the verification block to the user when it finishes.

```bash
# the batch this was built for
python3 .claude/skills/social-media-upload/scripts/ghl_social_upload.py \
    "C:/Users/footb/Downloads/services-posts"
```

## How it works

A batch folder straight out of the zip already contains everything:

```
32-missed-call-portrait.png      the images
ghl-facebook-services.csv        dates + captions + <<MEDIA_URL:id>> tokens
ghl-instagram-services.csv
```

So there is **nothing to regenerate and no captions file needed**. The CSVs
already hold the schedule and the per-platform copy; the only thing missing is
the image URLs. The script uploads each PNG and replaces each
`<<MEDIA_URL:id>>` token with the URL the Media Library returns for
`<id>-portrait.png`.

The replacement is a literal text substitution on the raw bytes, so the header
row, quoting, dates, BOM and line endings are untouched. This was verified
byte-identical to what the original `make_csv.py` generator produced for the
same batch — the substitution path is equivalent, without the external
dependency.

Two things are resolved internally and are **not** inputs:

- **credentials** — the repo `.env`
- **the URL registry** — `media-registry.json` next to the script

Both are located relative to the script, so the folder path stays the only
argument.

## Credentials

The token lives in the repo `.env` as **`GHL_API_KEY`** (a 40-char `pit-`
private integration token), with **`GHL_LOCATION_ID`** alongside it. Note the
name: an older version of this toolchain expected `GHL_TOKEN`, so the script
accepts either and bridges it in-process.

Never `export` the token in a shell command, never echo it, never paste it into
chat. All output passes through a redaction filter; keep it that way.

**On HTTP 401: stop. Do not retry.** It means the token lacks the `medias`
scope. Say so and let the user fix the scopes. URLs already uploaded are saved,
so a re-run resumes rather than duplicating.

## CSV format: Advanced is canonical

Two formats exist and the script detects which it is looking at. The
authoritative spec is `brand/ghl-csv-format.md` in the **aspired-brand-repo**
(confirmed 2026-10-17 against GHL's own `advance-sample.csv`).

| | Advanced | Basic |
|---|---|---|
| Status | **canonical — build this** | legacy, do not build again |
| Columns | 39 | 6 |
| Header rows | **2** (platform grouping, then names) | 1 |

**Row 1 is how GHL detects Advanced.** Without it the file is parsed as Basic
no matter which radio button is selected in the UI, and every
platform-specific column is silently ignored — no error. That is what cost
five rounds of debugging on the GBP Call button.

**The UI flips the radio back to Basic** when you click Advanced then Upload.
Ignore it; detection comes from row 1 of the file, not the radio.

Basic is still accepted by this script so older batches can be filled in, but
it prints a warning. Rebuild those with the upstream generator:

```bash
python3 render/ghl_csv.py --captions <captions.json> --media <media-registry.json>
```

**Three Advanced header names differ from GHL's help article.** The template
wins: `OGmetaUrl (url)` not `link (OGmetaUrl)`, `imageUrls (comma-separated)`
not `imageUrls`, `videoUrls (comma-separated)` not `videoUrls`, `category` not
`Category`.

**For GBP posts:** `eventType=call_to_action`, `actionType=call`, and
`actionUrl` **empty** — lowercase in CSV even though the JSON API docs show
uppercase. The button pulls the phone number off the Business Profile, so a URL
is neither needed nor accepted.

## Non-obvious things that will bite

**The headers are exact strings, parentheses included.** GHL rejects a bare
`postAtSpecificTime`, and rejects omitting a column you don't use — every
column must be present, empty where unused. Never "tidy" them. Four Advanced
columns are named `title` and three are named `type`, so Advanced files must be
read and written **positionally**; `csv.DictWriter` cannot represent that shape.

**Never hand-edit the CSVs, and never open them in Excel.** Excel rewrites the
date format on save and the import then fails or misschedules. Files are
`utf-8-sig` because a plain-UTF-8 caption imports into GHL silently mangled.

**`media-registry.json` is a running ledger across every batch — merge, never
overwrite.** It is the only thing preventing duplicate Media Library entries;
an id already present is skipped. It is saved after *each* upload, so a
mid-batch failure cannot lose URLs already earned. It holds 35 entries as of
the Dec-2026/Jan-2027 services batch (30 from the first batch + 5).

**The first patch writes a `.csv.bak`** holding the pristine token version, and
never overwrites an existing one. That is what makes a re-run always possible.

**Row order in a generated CSV is not post-id order.** It follows the schedule,
not the ids. In the first batch, row 2 was `03-three-seconds` and row 10 was
`28-dead-inbox`. This matters whenever mapping a CSV row back to a post.

**Verify by byte size, not by trust.** The CDN (`assets.cdn.filesafe.space`)
is public, so each uploaded URL can be fetched with **no auth header** and its
length compared to the local PNG. That check is what catches a mis-wired
id→URL mapping; the script runs it automatically for everything it uploaded.

**Both platforms share one image per post but have different captions.** One
upload per image, referenced from both CSVs.

**Re-running is safe.** Nothing to upload and no tokens left means it just
re-verifies and exits 0.

## If the registry is lost

Do not re-upload — that creates duplicate Media Library entries. If the URLs
still exist in a filled-in CSV, rebuild from it:

```bash
python3 .claude/skills/social-media-upload/scripts/rebuild_registry.py \
    "<folder-with-pngs>" "<filled-in-csv>"
```

It fingerprints each URL by `Content-Length` against the local PNGs, refuses to
run if byte sizes aren't unique, refuses to write a partial registry, and never
clobbers an existing entry.

## Verification before reporting done

The script asserts all of this and prints PASS/FAIL per line — relay it:

- format detected per file, printed as `[advanced format]` / `[basic format]`
- Advanced: row 1 is the 39-column grouping, row 2 the 39 names in order,
  every data row 39 wide
- Basic: the six legacy columns in order, plus a deprecation warning
- UTF-8 BOM present
- every row carries an image URL; every one is `https://`, no duplicates
- no `<<MEDIA_URL:` token anywhere in any file
- every date is `YYYY-MM-DD HH:mm:ss`
- each newly uploaded URL returns 200 / `image/png` with **no auth header**,
  byte length matching the local PNG

Columns are located **by name**, not by hardcoded index, since the two formats
disagree on position for some columns.

Exit code is non-zero if anything fails.

## Upstream

Images and token CSVs come from the **aspired-brand-repo**, which is a separate
repo from this Django project:

```
brand/ghl-csv-format.md   the authoritative CSV spec
brand/social-images.md    canvas sizes, layout anatomy, per-platform rules
render/render.py          renders the PNGs from posts.json
render/ghl_csv.py         builds Advanced CSVs from a captions file
render/out/               rendered PNGs + generated CSVs
```

Images are `<id>-<canvas>.png`; this skill handles the `-portrait` canvas
(1080x1350, Instagram and Facebook). Square (1080x1080) is for LinkedIn and X,
landscape (1200x628) for link posts. Portrait is never cropped to square —
re-render instead, since a centre crop takes the eyebrow and the handle.

That render step is outside this skill; this skill starts from the zipped batch
it produces. When the format spec there changes, update `ADV_HEAD` /
`ADV_GROUP` in the script to match — they are a verbatim copy.
