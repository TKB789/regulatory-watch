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
        "name": "Health Canada recalls & safety alerts",
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
        "name": "European Commission — Health news",
        "label": "EU Commission Health",
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
        print(f"  • {f.name}  (feed_id will be: '{f.stem.lower()}')", flush=True)
    print("", flush=True)

    # Parse each uploaded raw file
    new_items = []
    parsed_feed_ids = set()
    for raw_file in raw_files:
        # Strip extension — works for .xml, .rss, .atom, or no extension at all.
        # The "stem" is everything before the LAST dot.
        feed_id = raw_file.stem.lower()
        meta = FEED_META.get(feed_id)
        if not meta:
            valid_ids = ", ".join(sorted(FEED_META.keys()))
            print(f"  ⚠ {raw_file.name}: feed_id '{feed_id}' not recognized.", flush=True)
            print(f"     Valid feed_ids are: {valid_ids}", flush=True)
            continue

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

    # Preserve existing feed_status; mark the manually-fetched feeds as fresh
    now_iso = datetime.now(timezone.utc).isoformat()
    feed_status = existing_data.get("feed_status", {})
    for fid in parsed_feed_ids:
        feed_status[fid] = {
            "name": FEED_META[fid]["name"],
            "feed_id": fid,
            "status": "ok",
            "last_attempt": now_iso,
            "last_success": now_iso,
            "items_fetched": sum(1 for i in new_items if i["feed_id"] == fid),
            "source": "manual-fetch",
        }

    out = {
        "generated_at": now_iso,
        "item_count": len(all_items),
        "feed_count": existing_data.get("feed_count", len(FEED_META)),
        "new_this_run": new_count,
        "pruned_this_run": 0,
        "failed_feeds": existing_data.get("failed_feeds", []),
        "empty_feeds": existing_data.get("empty_feeds", []),
        "feed_status": feed_status,
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
        print(f"FATAL but recoverable: {e}", flush=True)
        sys.exit(0)
