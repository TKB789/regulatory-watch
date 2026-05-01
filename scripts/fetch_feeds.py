#!/usr/bin/env python3
"""
fetch_feeds.py — fetches RSS/Atom feeds from regulatory authorities
and writes a consolidated feed.json that the dashboard reads.

Run by GitHub Actions on a schedule. Free, no API keys needed.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser  # pip install feedparser

# ---------------------------------------------------------------------------
# FEED CONFIG
# ---------------------------------------------------------------------------
# Each feed is tagged with:
#   - agency: short code shown in the UI
#   - category: 'premarket' | 'postmarket' | 'guidance' | 'recall' | 'prepub'
#   - region: friendly label
#
# Add or remove feeds here — that's the only config file you'll edit.
# ---------------------------------------------------------------------------

FEEDS = [
    # --- US FDA ---
    {
        "agency": "FDA",
        "region": "United States",
        "category": "recall",
        "name": "FDA Medical Device Recalls",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medical-device-recalls/rss.xml",
    },
    {
        "agency": "FDA",
        "region": "United States",
        "category": "postmarket",
        "name": "FDA Medical Device Safety",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medical-device-safety/rss.xml",
    },
    {
        "agency": "FDA",
        "region": "United States",
        "category": "guidance",
        "name": "FDA Press Announcements",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    },
    # --- Federal Register (pre-publication / proposed rules) ---
    {
        "agency": "FR",
        "region": "United States",
        "category": "prepub",
        "name": "Federal Register — FDA documents",
        # Federal Register has a great public API. This URL filters for FDA documents.
        "url": "https://www.federalregister.gov/api/v1/documents.rss?conditions[agencies][]=food-and-drug-administration&conditions[type][]=RULE&conditions[type][]=PRORULE&conditions[type][]=NOTICE",
    },
    # --- MHRA (UK) ---
    {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "postmarket",
        "name": "MHRA news & alerts",
        "url": "https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
    },
    # --- Health Canada ---
    {
        "agency": "HC",
        "region": "Canada",
        "category": "recall",
        "name": "Health Canada recalls & safety alerts",
        "url": "https://recalls-rappels.canada.ca/en/rss.xml",
    },
    # --- TGA (Australia) ---
    {
        "agency": "TGA",
        "region": "Australia",
        "category": "postmarket",
        "name": "TGA news & alerts",
        "url": "https://www.tga.gov.au/rss.xml",
    },
    # --- European Commission — public health ---
    {
        "agency": "EMA",
        "region": "European Union",
        "category": "guidance",
        "name": "European Commission — Health news",
        "url": "https://health.ec.europa.eu/rss_en",
    },
]

# Keywords that suggest a Federal Register / news item is medical-device-related.
# Used to filter out drug-only or food-only items from broad feeds.
DEVICE_KEYWORDS = [
    "device", "510(k)", "pma", "de novo", "udi", "ivd",
    "diagnostic", "implant", "cdrh", "mdr", "ivdr", "mdcg",
    "premarket", "post-market", "postmarket", "recall",
    "vigilance", "psur", "estar", "notified body", "samd",
    "software as a medical", "ai/ml", "cybersecurity",
]

# How many items per feed (most recent)
MAX_ITEMS_PER_FEED = 15

# Total items in the consolidated output
MAX_TOTAL_ITEMS = 200

# ---------------------------------------------------------------------------


def is_device_related(text: str) -> bool:
    """Return True if the text looks medical-device related."""
    t = (text or "").lower()
    return any(k in t for k in DEVICE_KEYWORDS)


def parse_date(entry) -> str:
    """Return ISO-8601 date string for an entry, falling back to now."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


def clean_html(text: str) -> str:
    """Strip tags and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate(text: str, n: int = 400) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1].rsplit(" ", 1)[0] + "…"


def fetch_one(feed_cfg: dict) -> list:
    """Fetch a single feed and return a list of normalised items."""
    print(f"  → {feed_cfg['agency']:6s} {feed_cfg['name']}", flush=True)
    items = []
    try:
        parsed = feedparser.parse(feed_cfg["url"])
        if parsed.bozo and not parsed.entries:
            print(f"    ✗ feed error: {parsed.bozo_exception}", flush=True)
            return []

        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", entry.get("description", "")))
            link = entry.get("link", "")
            full_text = f"{title} {summary}"

            # For broad feeds (Federal Register, MHRA news), filter to device topics.
            if feed_cfg["agency"] in ("FR", "MHRA", "TGA", "EMA"):
                if not is_device_related(full_text):
                    continue

            items.append({
                "title": title,
                "summary": truncate(summary, 500),
                "link": link,
                "published": parse_date(entry),
                "agency": feed_cfg["agency"],
                "region": feed_cfg["region"],
                "category": feed_cfg["category"],
                "source": feed_cfg["name"],
            })
        print(f"    ✓ {len(items)} item(s)", flush=True)
    except Exception as e:
        print(f"    ✗ exception: {e}", flush=True)
    return items


def main():
    print(f"Fetching {len(FEEDS)} feed(s)…", flush=True)
    all_items = []
    for cfg in FEEDS:
        all_items.extend(fetch_one(cfg))
        time.sleep(0.5)  # be polite

    # Sort newest first, cap total
    all_items.sort(key=lambda x: x["published"], reverse=True)
    all_items = all_items[:MAX_TOTAL_ITEMS]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(all_items),
        "feed_count": len(FEEDS),
        "items": all_items,
    }

    out_path = Path(__file__).parent.parent / "data" / "feed.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(all_items)} item(s) → {out_path}", flush=True)


if __name__ == "__main__":
    main()
