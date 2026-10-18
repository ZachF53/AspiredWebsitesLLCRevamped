#!/usr/bin/env python3
"""Upload a batch of rendered social images to the GoHighLevel Media Library and
fill in the image URLs in that batch's Social Planner CSVs.

    python3 ghl_social_upload.py <path-to-unzipped-folder>

The folder path is the ONLY input. A batch folder straight out of the zip holds
everything needed:

    32-missed-call-portrait.png   ...the images
    ghl-facebook-services.csv     ...dates + captions + <<MEDIA_URL:id>> tokens
    ghl-instagram-services.csv

So there is nothing to regenerate and no captions file to find. Each
<<MEDIA_URL:id>> token is replaced with the URL the Media Library returns for
<id>-portrait.png. The replacement is a literal text substitution on the raw
bytes, which leaves the header row, the quoting, the dates, the BOM and the
line endings untouched - safer than round-tripping through a CSV writer.

Two things are resolved internally and are not inputs:
  - credentials      the repo .env (GHL_API_KEY + GHL_LOCATION_ID)
  - the URL registry media-registry.json next to this script

The token is bridged in-process, never exported to a shell, never written to
disk, and every printed line goes through redact().
"""

import argparse, json, re, sys, time
from pathlib import Path

import requests

API = "https://services.leadconnectorhq.com/medias/upload-file"
VERSION = "2021-07-28"

# Two CSV formats exist. ADVANCED is canonical as of 2026-10-17; see
# brand/ghl-csv-format.md in the aspired-brand-repo. BASIC is legacy and is
# still accepted here only so older batches can be filled in.
#
# The parentheticals are PART OF THE HEADER NAME, not documentation. GHL's
# import validator rejects a bare "postAtSpecificTime".

# Row 1 of an Advanced file: the platform grouping. This row is how GHL
# detects Advanced; without it the file is parsed as Basic no matter which
# radio button is selected, and every platform-specific column is silently
# ignored.
ADV_GROUP = (["All Social"] * 11 + ["Facebook"] + ["Instagram"] + ["LinkedIn"] * 2
             + ["Google (GBP)"] * 10 + ["YouTube"] * 3 + ["TikTok"] * 7
             + ["Community"] * 2 + ["Pinterest"] * 2)

# Row 2: the 39 column names, verbatim from GHL's own advance-sample.csv.
# Four columns are named "title" and three "type", so files must be read and
# written positionally - csv.DictWriter cannot represent this shape.
ADV_HEAD = [
    "postAtSpecificTime (YYYY-MM-DD HH:mm:ss)", "content", "OGmetaUrl (url)",
    "imageUrls (comma-separated)", "gifUrl", "videoUrls (comma-separated)",
    "mediaOptimization (true/false)", "applyWatermark (true/false)",
    "tags (comma-separated)", "category", "followUpComment",
    "type (post/story/reel)", "type (post/story/reel)",
    "pdfTitle", "postAsPdf (true/false)",
    "eventType (call_to_action/event/offer)",
    "actionType (none/order/book/shop/learn_more/call/sign_up)",
    "title", "offerTitle", "startDate (YYYY-MM-DD HH:mm:ss)",
    "endDate (YYYY-MM-DD HH:mm:ss)", "termsConditions", "couponCode",
    "redeemOnlineUrl", "actionUrl",
    "title", "privacyLevel (private/public/unlisted)", "type (video/short)",
    "privacyLevel (everyone/friends/only_me)", "promoteOtherBrand (true/false)",
    "enableComment (true/false)", "enableDuet (true/false)",
    "enableStitch (true/false)", "videoDisclosure (true/false)",
    "promoteYourBrand (true/false)",
    "title", "notifyAllGroupMembers (true/false)", "title", "link",
]

# Legacy 6-column format. Note the header names differ from Advanced beyond
# just the count: "link (OGmetaUrl)" vs "OGmetaUrl (url)", and bare
# "imageUrls" vs "imageUrls (comma-separated)".
BASIC_HEAD = [
    "postAtSpecificTime (YYYY-MM-DD HH:mm:ss)",
    "content",
    "link (OGmetaUrl)",
    "imageUrls",
    "gifUrl",
    "videoUrls",
]

assert len(ADV_GROUP) == len(ADV_HEAD) == 39

TOKEN_RE = re.compile(r"<<MEDIA_URL:([^>]+)>>")

HERE = Path(__file__).resolve()
SKILL_DIR = HERE.parents[1]
REPO_ROOT = HERE.parents[4]
REGISTRY = SKILL_DIR / "media-registry.json"

SECRETS: list[str] = []


def redact(text) -> str:
    text = str(text)
    for s in SECRETS:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def say(text=""):
    print(redact(text))


def die(text):
    say(f"\nERROR: {text}")
    sys.exit(1)


# --------------------------------------------------------------------------
def load_creds(env_path: Path):
    if not env_path.exists():
        die(f"no .env at {env_path}")
    cfg = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")

    SECRETS.extend(x for x in (cfg.get("GHL_API_KEY"), cfg.get("GHL_TOKEN")) if x)
    token = cfg.get("GHL_API_KEY") or cfg.get("GHL_TOKEN")
    location = cfg.get("GHL_LOCATION_ID")
    if not token:
        die("GHL_API_KEY (or GHL_TOKEN) not found in .env")
    if not location:
        die("GHL_LOCATION_ID not found in .env")
    return token, location


def upload_one(path: Path, token: str, location: str) -> dict:
    with path.open("rb") as fh:
        r = requests.post(
            API,
            headers={"Authorization": f"Bearer {token}", "Version": VERSION},
            files={"file": (path.name, fh, "image/png")},
            data={"hosted": "false", "altType": "location", "altId": location},
            timeout=120,
        )
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:500]}
    return {"status": r.status_code, "body": body}


def find_url(body):
    if isinstance(body, dict):
        for key in ("url", "fileUrl", "link", "publicUrl"):
            v = body.get(key)
            if isinstance(v, str) and v.startswith("https://"):
                return v
        for v in body.values():
            if (found := find_url(v)):
                return found
    elif isinstance(body, list):
        for v in body:
            if (found := find_url(v)):
                return found
    return None


# --------------------------------------------------------------------------
def read_text(path: Path):
    raw = path.read_bytes()
    return raw.decode("utf-8-sig"), raw.startswith(b"\xef\xbb\xbf")


def patch_csv(path: Path, media: dict) -> int:
    """Swap <<MEDIA_URL:id>> for the real url, preserving every other byte."""
    text, had_bom = read_text(path)
    ids = TOKEN_RE.findall(text)
    if not ids:
        return 0

    # keep the pristine token version once, so a re-run is always possible
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        bak.write_bytes(path.read_bytes())

    n = 0
    for pid in dict.fromkeys(ids):
        url = media.get(pid)
        if not url:
            continue
        token = f"<<MEDIA_URL:{pid}>>"
        n += text.count(token)
        text = text.replace(token, url)

    out = (b"\xef\xbb\xbf" if had_bom else b"") + text.encode("utf-8")
    path.write_bytes(out)
    return n


def read_table(path: Path):
    """Return (fmt, group_row, header, data_rows).

    Advanced files carry TWO header rows: a platform grouping then the column
    names. Basic carries one. Row 1 being "All Social" is the signature GHL
    itself keys off.
    """
    import csv as _csv
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in _csv.reader(fh) if any(c.strip() for c in r)]
    if not rows:
        return "empty", [], [], []
    if rows[0] and rows[0][0].strip() == "All Social":
        return "advanced", rows[0], (rows[1] if len(rows) > 1 else []), rows[2:]
    return "basic", [], rows[0], rows[1:]


def col_index(header, prefix):
    for i, h in enumerate(header):
        if h.strip().startswith(prefix):
            return i
    return None


def verify(csvs, media, folder, uploaded_ids):
    ok = True
    warned_basic = False

    def chk(cond, label):
        nonlocal ok
        ok = ok and bool(cond)
        say(("  PASS  " if cond else "  FAIL  ") + label)

    say("\n=== CSV structure ===")
    counts = []
    for path in csvs:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        fmt, group, hdr, rows = read_table(path)
        counts.append(len(rows))
        say(f"{path.name}   [{fmt} format]")
        chk(raw[:3] == b"\xef\xbb\xbf", "utf-8 BOM present")

        if fmt == "advanced":
            chk(group == ADV_GROUP, "row 1 = the 39-column platform grouping")
            chk(hdr == ADV_HEAD, "row 2 = the 39 column names, in order")
            chk(all(len(r) == 39 for r in rows), "every data row is 39 columns wide")
        elif fmt == "basic":
            chk(hdr == BASIC_HEAD, "header = the six legacy Basic columns, in order")
            warned_basic = True
        else:
            chk(False, f"unrecognised format in {path.name}")
            continue

        chk(len(rows) > 0, f"has rows ({len(rows)})")

        # look the columns up by name - index differs between the formats for
        # some columns, and hardcoding positions is how the GBP bug happened
        i_img = col_index(hdr, "imageUrls")
        i_when = col_index(hdr, "postAtSpecificTime")
        if i_img is None or i_when is None:
            chk(False, "could not locate imageUrls / postAtSpecificTime columns")
            continue

        imgs = [r[i_img] for r in rows if len(r) > i_img and r[i_img].strip()]
        chk(len(imgs) == len(rows), "every row carries an image url")
        chk(all(u.startswith("https://") for u in imgs), "every imageUrls starts with https://")
        chk("<<MEDIA_URL:" not in text, "no <<MEDIA_URL: token anywhere in file")
        chk(len(set(imgs)) == len(imgs), "no duplicate image urls")
        bad = [r[i_when] for r in rows
               if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", r[i_when])]
        chk(not bad, "every date is YYYY-MM-DD HH:mm:ss" + (f" (bad: {bad})" if bad else ""))

    if len(set(counts)) == 1:
        chk(True, f"all files agree on row count ({counts[0]})")
    else:
        say(f"  note  row counts differ between files: {counts}")
        say("        (expected when platforms carry different post sets)")

    if warned_basic:
        say("\n  WARNING: Basic format detected.")
        say("  Advanced is canonical as of 2026-10-17 (brand/ghl-csv-format.md).")
        say("  Basic silently drops every platform-specific column - a GBP post")
        say("  imports with its action button set to None and GHL reports no error.")
        say("  Rebuild with: python3 render/ghl_csv.py --captions <f> --media <registry>")

    # Every URL uploaded this run must serve the image it claims to. Byte size
    # is the fingerprint; this is what catches a mis-wired id -> url mapping.
    if uploaded_ids:
        say("\n=== each new URL serves its own image (no auth header) ===")
        for pid in uploaded_ids:
            local = (folder / f"{pid}-portrait.png").stat().st_size
            try:
                r = requests.get(media[pid], timeout=30)
            except Exception as e:
                chk(False, f"{pid}: fetch failed: {type(e).__name__}")
                continue
            ct = r.headers.get("content-type", "")
            good = r.status_code == 200 and ct == "image/png" and len(r.content) == local
            chk(good, f"{pid:<20} HTTP {r.status_code}  {ct}  {len(r.content)}b == local {local}b")
    return ok


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Upload a social batch to GHL and fill in its CSVs.")
    ap.add_argument("folder", help="the unzipped batch folder - the only input")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--pause", type=float, default=3.0, help="seconds between uploads")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        die(f"not a directory: {folder}")

    pngs = sorted(folder.glob("*-portrait.png"))
    csvs = sorted(p for p in folder.glob("ghl-*.csv")
                  if p.suffix == ".csv" and "template" not in p.name)
    if not pngs:
        die(f"no *-portrait.png in {folder}")
    if not csvs:
        die(f"no ghl-*.csv in {folder} - the batch should ship its CSVs")

    # The registry is a running ledger across every batch; merge, never
    # overwrite, or earlier batches lose their urls and get re-uploaded.
    media = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
    before = len(media)

    todo = [p for p in pngs if p.name.replace("-portrait.png", "") not in media]

    # what the CSVs still ask for
    pending = set()
    for c in csvs:
        pending.update(TOKEN_RE.findall(read_text(c)[0]))

    say(f"batch      {folder}")
    say(f"csvs       {', '.join(c.name for c in csvs)}")
    say(f"registry   {REGISTRY.name}  ({before} urls)")
    say(f"images     {len(pngs)} found, {len(todo)} need uploading")
    say(f"tokens     {len(pending)} placeholder(s) to fill\n")

    have = {p.name.replace("-portrait.png", "") for p in pngs} | set(media)
    if (orphans := sorted(pending - have)):
        die("CSV asks for ids with no image in the folder and no stored url: "
            + ", ".join(orphans))

    if args.dry_run:
        for p in todo:
            say(f"  would upload  {p.name}")
        for pid in sorted(pending):
            say(f"  would fill    <<MEDIA_URL:{pid}>>")
        say("\n--dry-run: nothing uploaded, no files changed")
        return

    uploaded = []
    if todo:
        token, location = load_creds(REPO_ROOT / ".env")
        for i, p in enumerate(todo):
            pid = p.name.replace("-portrait.png", "")
            res = upload_one(p, token, location)
            url = find_url(res["body"]) if res["status"] < 300 else None

            if i == 0:
                say("--- full response, first upload (token redacted) ---")
                say(f"HTTP {res['status']}")
                say(json.dumps(res["body"], indent=2))
                say("--- end response ---\n")

            if url:
                media[pid] = url
                uploaded.append(pid)
                # save after EACH upload so a later failure cannot lose urls
                REGISTRY.write_text(json.dumps(media, indent=2) + "\n")
                say(f"  ok    {pid}")
                say(f"        {url}")
            else:
                say(f"  FAIL  {pid}  (HTTP {res['status']})")
                say(f"        {json.dumps(res['body'])[:600]}")
                if res["status"] == 401:
                    say("\n401 - the token likely lacks the 'medias' scope.")
                    say("Not retrying. Fix the scopes and run again; urls")
                    say("already uploaded are saved in the registry.")
                sys.exit(1)

            if i < len(todo) - 1 and args.pause:
                time.sleep(args.pause)
        say(f"\nregistry now holds {len(media)} urls (was {before})")
    else:
        say("Nothing to upload; every image already has a stored url.")

    say("\n=== filling in CSVs ===")
    for c in csvs:
        n = patch_csv(c, media)
        say(f"  {c.name}  ({n} url{'' if n == 1 else 's'} filled)")

    ok = verify(csvs, media, folder, uploaded)
    say("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    say("Do NOT open these CSVs in Excel - it rewrites the date format on save.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
