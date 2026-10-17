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

## Non-obvious things that will bite

**The CSV headers are exact strings, parentheses included.** GHL's import
validator rejects a bare `postAtSpecificTime`, and rejects omitting a column
you don't use — all six must be present, empty where unused. Never "tidy" them.

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

- header is the six required columns, in order, with the parentheses
- UTF-8 BOM present
- every `imageUrls` is `https://`, no duplicates
- no `<<MEDIA_URL:` token anywhere in either file
- every date is `YYYY-MM-DD HH:mm:ss`
- both files agree on row count
- each newly uploaded URL returns 200 / `image/png` with **no auth header**,
  byte length matching the local PNG

Exit code is non-zero if anything fails.

## Upstream

The PNGs and the token CSVs come from a separate render step
(`make_csv.py` + a `*-captions.json` schedule). That step is outside this
skill; this skill starts from the zipped batch it produces.
