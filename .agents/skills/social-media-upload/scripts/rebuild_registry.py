#!/usr/bin/env python3
"""Rebuild a lost media.json from an already-generated CSV, without re-uploading.

    python3 rebuild_registry.py <folder-with-pngs> <old-csv> [--out media.json]

Why this exists: media.json went missing once while 30 already-uploaded URLs
were still sitting in a generated CSV. Re-uploading would have created 30
duplicate Media Library entries.

How it maps url -> post id: the CDN is public, so each URL's Content-Length is
compared against the local PNG byte sizes. Sizes are verified unique first, so
a match is a fingerprint rather than a guess.

Do NOT map by row order. The generated CSV is ordered by the schedule in the
captions file, which is NOT post-id order - on the batch this was built for,
row 2 was 03-three-seconds and row 10 was 28-dead-inbox. Assuming row order
would have mislabeled 23 of 30 and pointed live posts at the wrong images.
"""

import argparse, collections, csv, json, sys
from pathlib import Path

import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="folder holding the *-portrait.png files")
    ap.add_argument("old_csv", help="a previously generated CSV with real urls")
    ap.add_argument("--out", help="registry to merge into (default: the skill's media-registry.json)")
    ap.add_argument("--column", default="imageUrls")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    default_registry = Path(__file__).resolve().parents[1] / "media-registry.json"
    out_path = Path(args.out) if args.out else default_registry

    sizes = {p.stat().st_size: p.name.replace("-portrait.png", "")
             for p in folder.glob("*-portrait.png")}
    all_sizes = [p.stat().st_size for p in folder.glob("*-portrait.png")]
    if not all_sizes:
        sys.exit(f"no *-portrait.png in {folder}")

    dupes = [s for s, c in collections.Counter(all_sizes).items() if c > 1]
    if dupes:
        sys.exit("ABORT: these byte sizes are not unique, so size cannot "
                 f"fingerprint the mapping: {dupes}")
    print(f"{len(sizes)} local pngs, all byte sizes unique\n")

    with Path(args.old_csv).open(newline="", encoding="utf-8-sig") as fh:
        urls = [r[args.column] for r in csv.DictReader(fh)]

    found, problems = {}, []
    for i, url in enumerate(urls, 1):
        if not url.startswith("https://"):
            problems.append(f"row {i}: not a url: {url[:40]}")
            continue
        try:
            r = requests.head(url, timeout=30, allow_redirects=True)
        except Exception as e:
            problems.append(f"row {i}: HEAD failed: {type(e).__name__}")
            continue
        if r.status_code != 200:
            problems.append(f"row {i}: HTTP {r.status_code}")
            continue
        length = int(r.headers.get("content-length", -1))
        pid = sizes.get(length)
        if not pid:
            problems.append(f"row {i}: {length} bytes matches no local png")
            continue
        if pid in found:
            problems.append(f"row {i}: {pid} matched twice")
            continue
        found[pid] = url
        print(f"  row {i:>3}  {pid:<24} {length:>8} b")

    print(f"\nmatched {len(found)}/{len(urls)}")

    order = sorted(sizes.values(), key=lambda s: int(s.split("-")[0]))
    print("agrees with naive row order:", dict(zip(order, urls)) == found,
          "(False is normal - that is the whole point)")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  " + p)
        sys.exit("\nNot writing a partial registry. Resolve the above first.")

    merged = json.loads(out_path.read_text()) if out_path.exists() else {}
    before = len(merged)
    for k, v in found.items():
        merged.setdefault(k, v)   # never clobber an existing entry
    out_path.write_text(json.dumps(merged, indent=2) + "\n")
    print(f"\n{out_path}: {before} -> {len(merged)} urls")


if __name__ == "__main__":
    main()
