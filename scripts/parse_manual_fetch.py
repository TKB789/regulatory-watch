#!/usr/bin/env python3
"""
Parses raw RSS/Atom feed files uploaded to manual-fetch/raw/ and merges
them into data/feed.json.

The filename of each raw file must match the feed_id, e.g.:
    manual-fetch/raw/fda-recalls.xml
    manual-fetch/raw/mhra-news.xml
    manual-fetch/raw/who-alerts.xml

This script intentionally only ADDS items to the archive — it never deletes
or prunes — so a partial manual fetch can't damage your data. If a feed_id
is missing from the upload, that feed simply isn't refreshed this round.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser

# Feed metadata, keyed by feed_id. This mirrors the FEEDS list in
# fetch_feeds.py but only the fields needed for parsing. Filename in
# manual-fetch/raw/ MUST match the feed_id.
FEED_META = {
    "fda-recalls": {
        "agency": "FDA",
        "region": "United States",
        "category": "recall",
        "name": "FDA Recalls",
        "label": "FDA Recalls",
    },
    "fda-medwatch": {
        "agency": "FDA",
        "region": "United States",
        "category": "postmarket",
        "name": "FDA MedWatch Safety Alerts",
        "label": "FDA MedWatch",
    },
    "fda-press": {
        "agency": "FDA",
        "region": "United States",
        "category": "guidance",
        "name": "FDA Press Announcements",
        "label": "FDA Press",
    },
    "federal-register": {
        "agency": "FR",
        "region": "United States",
        "category": "prepub",
        "name": "Federal Register — FDA documents",
        "label": "Federal Register",
    },
    "imdrf-wg": {
        "agency": "IMDRF",
        "region": "International",
        "category": "guidance",
        "name": "IMDRF Working Groups",
        "label": "IMDRF Working Groups",
    },
    "imdrf-consult": {
        "agency": "IMDRF",
        "region": "International",
        "category": "guidance",
        "name": "IMDRF Consultations",
        "label": "IMDRF Consultations",
    },
    "who-alerts": {
        # NOTE: WHO feed was dropped — no real upstream URL exists. This entry
        # is preserved so any old uploaded WHO files still parse cleanly into
        # the archive. Won't be fetched from anywhere new.
        "agency": "WHO",
        "region": "International",
        "category": "recall",
        "name": "WHO Medical Product Alerts",
        "label": "WHO Alerts",
    },
    "mhra-news": {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "postmarket",
        "name": "MHRA news & alerts",
        "label": "MHRA News",
    },
    "uk-alerts": {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "recall",
        "name": "UK Drug & Medical Device Alerts",
        "label": "UK Drug & Device Alerts",
    },
    "hc-recalls": {
        "agency": "HC",
        "region": "Canada",
        "category": "recall",
        "name": "Health Canada Medical Device Recalls",
        "label": "Health Canada Recalls",
    },
    "tga-news": {
        "agency": "TGA",
        "region": "Australia",
        "category": "postmarket",
        "name": "TGA news & alerts",
        "label": "TGA News",
    },
    "eu-health": {
        "agency": "EMA",
        "region": "European Union",
        "category": "guidance",
        "name": "EMA News & Press Releases",
        "label": "EMA News",
    },
    "eur-lex": {
        "agency": "EU",
        "region": "European Union",
        "category": "prepub",
        "name": "EUR-Lex — Recent regulations & directives",
        "label": "EUR-Lex",
    },
}

MAX_TOTAL_ITEMS = 1000

# Common filename variations that should map to canonical feed_ids.
# Lets users upload e.g. "hc.xml" or "health-canada.rss" and have it work.
FILENAME_ALIASES = {
    # FDA variants
    "fda-recall": "fda-recalls",
    "fda_recalls": "fda-recalls",
    "fda": "fda-recalls",  # bare "fda" assumed to be the most common
    "medwatch": "fda-medwatch",
    "fda_medwatch": "fda-medwatch",
    "fda-medwatch-safety": "fda-medwatch",
    "fda-medwatch-safety-alerts": "fda-medwatch",
    "fda_press": "fda-press",
    "fda-press-announcements": "fda-press",
    "fda-press-release": "fda-press",
    "fda-press-releases": "fda-press",
    # Federal Register variants
    "federal_register": "federal-register",
    "federalregister": "federal-register",
    "fr": "federal-register",
    # IMDRF variants
    "imdrf": "imdrf-wg",
    "imdrf-working-groups": "imdrf-wg",
    "imdrf_wg": "imdrf-wg",
    "imdrf-consultations": "imdrf-consult",
    "imdrf_consult": "imdrf-consult",
    # WHO variants
    "who": "who-alerts",
    "who-medical-product-alerts": "who-alerts",
    "who_alerts": "who-alerts",
    # MHRA variants
    "mhra": "mhra-news",
    "mhra_news": "mhra-news",
    # UK alerts variants
    "uk_alerts": "uk-alerts",
    "uk-drug-device-alerts": "uk-alerts",
    "drug-device-alerts": "uk-alerts",
    # Health Canada variants — common cause of confusion
    "hc": "hc-recalls",
    "hc_recalls": "hc-recalls",
    "health-canada": "hc-recalls",
    "health_canada": "hc-recalls",
    "healthcanada": "hc-recalls",
    "canada": "hc-recalls",
    # TGA variants
    "tga": "tga-news",
    "tga_news": "tga-news",
    # EU variants
    "eu": "eu-health",
    "eu-commission-health": "eu-health",
    "european-commission": "eu-health",
    "eu_health": "eu-health",
    # EUR-Lex variants
    "eur_lex": "eur-lex",
    "eurlex": "eur-lex",
}


def resolve_feed_id(filename_stem: str) -> str:
    """
    Take a filename (without extension) and return the canonical feed_id,
    or the original lowercase stem if no match. Strips common suffixes like
    timestamps so 'fda-recalls-2026-05-12.xml' still resolves to 'fda-recalls'.
    """
    raw = filename_stem.lower().strip()
    # Exact match against canonical feed_ids first
    if raw in FEED_META:
        return raw
    # Alias match
    if raw in FILENAME_ALIASES:
        return FILENAME_ALIASES[raw]
    # Strip trailing timestamps like "-2026-05-12" or "_2026_05_12"
    import re
    stripped = re.sub(r"[-_]\d{4}[-_]\d{1,2}[-_]\d{1,2}.*$", "", raw)
    if stripped in FEED_META:
        return stripped
    if stripped in FILENAME_ALIASES:
        return FILENAME_ALIASES[stripped]
    return raw


def strip_html(s: str) -> str:
    """Remove HTML tags from summary text."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalise_entry(entry, feed_id: str, meta: dict) -> dict:
    """Convert a feedparser entry into our standard item dict."""
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    link = (entry.get("link") or "").strip()
    summary = strip_html(entry.get("summary") or entry.get("description") or "")
    # Truncate summary to avoid bloating feed.json
    if len(summary) > 800:
        summary = summary[:800] + "…"

    # Published timestamp
    published_iso = None
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                published_iso = dt.isoformat()
                break
            except Exception:
                pass
    if not published_iso:
        published_iso = entry.get("published") or entry.get("updated") or ""

    return {
        "title": title,
        "summary": summary,
        "link": link,
        "published": published_iso,
        "agency": meta["agency"],
        "region": meta["region"],
        "category": meta["category"],
        "feed_id": feed_id,
        "feed_label": meta["label"],
        "source": meta["name"],
    }


def main():
    raw_dir = Path(__file__).parent.parent / "manual-fetch" / "raw"
    feed_path = Path(__file__).parent.parent / "data" / "feed.json"

    if not raw_dir.exists():
        print(f"⚠ No manual-fetch/raw directory found, nothing to do.", flush=True)
        return

    raw_files = sorted([f for f in raw_dir.iterdir() if f.is_file() and not f.name.startswith(".")])
    if not raw_files:
        print(f"⚠ No files in manual-fetch/raw/, nothing to do.", flush=True)
        return

    print(f"Found {len(raw_files)} raw file(s) to parse:", flush=True)
    for f in raw_files:
        resolved = resolve_feed_id(f.stem.lower())
        if resolved in FEED_META:
            print(f"  • {f.name}  → feed_id: '{resolved}' (recognized)", flush=True)
        else:
            print(f"  • {f.name}  → '{resolved}' (UNRECOGNIZED — will be skipped)", flush=True)
    print("", flush=True)

    # Parse each uploaded raw file
    new_items = []
    parsed_feed_ids = set()
    for raw_file in raw_files:
        # Resolve filename to canonical feed_id (handles aliases & timestamps).
        # E.g. "hc.xml", "health-canada.rss", "fda-recalls-2026-05-12.xml" all
        # resolve to their canonical feed_id.
        raw_stem = raw_file.stem.lower()
        feed_id = resolve_feed_id(raw_stem)
        meta = FEED_META.get(feed_id)
        if not meta:
            valid_ids = ", ".join(sorted(FEED_META.keys()))
            print(f"  ⚠ {raw_file.name}: feed_id '{feed_id}' not recognized.", flush=True)
            if raw_stem != feed_id:
                print(f"     (Tried alias resolution from '{raw_stem}')", flush=True)
            print(f"     Valid feed_ids are: {valid_ids}", flush=True)
            print(f"     Aliases include: hc / health-canada → hc-recalls, fda / medwatch → fda-medwatch, etc.", flush=True)
            continue
        # Confirm in log if we used an alias
        if raw_stem != feed_id:
            print(f"  ↳ {raw_file.name}: alias '{raw_stem}' → resolved to '{feed_id}'", flush=True)

        try:
            raw_bytes = raw_file.read_bytes()
            # Strip BOM and leading whitespace, just like fetch_feeds.py
            if raw_bytes[:3] == b"\xef\xbb\xbf":
                raw_bytes = raw_bytes[3:]
            raw_bytes = raw_bytes.lstrip()

            parsed = feedparser.parse(raw_bytes)
            entries = parsed.entries or []
            count = 0
            for entry in entries:
                item = normalise_entry(entry, feed_id, meta)
                if item:
                    new_items.append(item)
                    count += 1
            parsed_feed_ids.add(feed_id)
            print(f"  ✓ {raw_file.name} → {count} item(s)", flush=True)
        except Exception as e:
            print(f"  ✗ {raw_file.name}: parse failed — {e}", flush=True)

    if not new_items:
        print(f"\n⚠ No items extracted. Leaving feed.json unchanged.", flush=True)
        return

    # Load existing feed.json (append-only — never wipe)
    existing_items = []
    existing_data = {}
    if feed_path.exists():
        try:
            existing_data = json.loads(feed_path.read_text(encoding="utf-8"))
            existing_items = existing_data.get("items", [])
        except Exception as e:
            print(f"⚠ Couldn't read existing feed.json ({e}), starting fresh", flush=True)

    # Merge: new items take precedence over existing ones with the same link
    def key(item):
        return (item.get("link") or item.get("title") or "").strip().lower()

    seen = {}
    for item in new_items:
        k = key(item)
        if k:
            seen[k] = item
    new_count = 0
    existing_keys = {key(i) for i in existing_items if key(i)}
    for item in existing_items:
        k = key(item)
        if k and k not in seen:
            seen[k] = item
    new_count = sum(1 for k in seen if k not in existing_keys)

    all_items = list(seen.values())
    # Sort newest first, cap total
    all_items.sort(key=lambda x: x.get("published") or "", reverse=True)
    if len(all_items) > MAX_TOTAL_ITEMS:
        all_items = all_items[:MAX_TOTAL_ITEMS]

    # Preserve existing feed_status; mark the manually-fetched feeds as fresh.
    #
    # feed_status is stored as a LIST of objects in feed.json (to match the
    # schema written by scripts/fetch_feeds.py). We convert to a dict keyed by
    # feed_id for easy lookup/update, then convert back to a list before write.
    now_iso = datetime.now(timezone.utc).isoformat()
    existing_status = existing_data.get("feed_status", [])
    status_by_id = {}
    # Handle both list-of-objects (correct schema) and dict-keyed-by-id (older format)
    if isinstance(existing_status, list):
        for s in existing_status:
            if isinstance(s, dict) and s.get("feed_id"):
                status_by_id[s["feed_id"]] = s
    elif isinstance(existing_status, dict):
        for fid, s in existing_status.items():
            if isinstance(s, dict):
                status_by_id[fid] = s

    # Update entries for the manually-fetched feeds
    for fid in parsed_feed_ids:
        status_by_id[fid] = {
            "name": FEED_META[fid]["name"],
            "feed_id": fid,
            "status": "ok",
            "last_attempt": now_iso,
            "last_success": now_iso,
            "items_fetched": sum(1 for i in new_items if i["feed_id"] == fid),
            "source": "manual-fetch",
        }

    # Convert back to a list for output (matches fetch_feeds.py schema)
    feed_status_list = list(status_by_id.values())

    out = {
        "generated_at": now_iso,
        "item_count": len(all_items),
        "feed_count": existing_data.get("feed_count", len(FEED_META)),
        "new_this_run": new_count,
        "pruned_this_run": 0,
        "failed_feeds": existing_data.get("failed_feeds", []),
        "empty_feeds": existing_data.get("empty_feeds", []),
        "feed_status": feed_status_list,
        "manual_fetch_at": now_iso,
        "manual_fetched_feeds": sorted(parsed_feed_ids),
        "items": all_items,
    }

    feed_path.parent.mkdir(exist_ok=True)
    feed_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"\n✓ Wrote {len(all_items)} item(s) → {feed_path} "
        f"({new_count} new from manual fetch, "
        f"{len(parsed_feed_ids)} feed(s) refreshed)",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Print full traceback so we can actually see what broke.
        import traceback
        print(f"\n✗ FATAL: {e}", flush=True)
        print("Traceback:", flush=True)
        traceback.print_exc()
        # Exit with error status so the GitHub Actions workflow shows a red X
        # instead of green check — silent failures hide real bugs.
        sys.exit(1)
