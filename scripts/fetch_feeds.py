#!/usr/bin/env python3
"""
fetch_feeds.py — fetches RSS/Atom feeds from regulatory authorities
and writes a consolidated feed.json that the dashboard reads.

Run by GitHub Actions on a schedule. Free, no API keys needed.
"""

import gzip
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser  # pip install feedparser

# Hard timeout for any single network request (seconds).
# Without this, a slow feed can hang the entire workflow.
socket.setdefaulttimeout(30)

# Many gov RSS feeds block default Python/feedparser User-Agents. We send a
# realistic browser-like UA to get past basic bot detection.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# FEED CONFIG
# ---------------------------------------------------------------------------
# Each feed is tagged with:
#   - agency:   short code shown in the UI
#   - region:   friendly label
#   - category: 'premarket' | 'postmarket' | 'guidance' | 'recall' | 'prepub'
#   - feed_id:  stable identifier the dashboard can key off (don't rename)
#   - label:    short display name for UI
#   - name:     full descriptive name (also used as the legacy 'source' field)
# ---------------------------------------------------------------------------

FEEDS = [
    # --- US FDA ---
    {
        "agency": "FDA",
        "region": "United States",
        "category": "recall",
        "feed_id": "fda-recalls",
        "label": "FDA Recalls",
        "name": "FDA Recalls",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml",
    },
    {
        "agency": "FDA",
        "region": "United States",
        "category": "postmarket",
        "feed_id": "fda-medwatch",
        "label": "FDA MedWatch",
        "name": "FDA MedWatch Safety Alerts",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml",
    },
    {
        "agency": "FDA",
        "region": "United States",
        "category": "guidance",
        "feed_id": "fda-press",
        "label": "FDA Press",
        "name": "FDA Press Announcements",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    },
    # --- Federal Register (pre-publication / proposed rules) ---
    {
        "agency": "FR",
        "region": "United States",
        "category": "prepub",
        "feed_id": "federal-register",
        "label": "Federal Register",
        "name": "Federal Register — FDA documents",
        "url": "https://www.federalregister.gov/api/v1/documents.rss?conditions[agencies][]=food-and-drug-administration&conditions[type][]=RULE&conditions[type][]=PRORULE&conditions[type][]=NOTICE",
    },
    # --- IMDRF (international harmonization) ---
    {
        "agency": "IMDRF",
        "region": "International",
        "category": "guidance",
        "feed_id": "imdrf-wg",
        "label": "IMDRF Working Groups",
        "name": "IMDRF Working Groups",
        "url": "https://www.imdrf.org/working-groups.xml",
    },
    {
        "agency": "IMDRF",
        "region": "International",
        "category": "guidance",
        "feed_id": "imdrf-consult",
        "label": "IMDRF Consultations",
        "name": "IMDRF Consultations",
        "url": "https://www.imdrf.org/consultations.xml",
    },
    # --- MHRA (UK) ---
    {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "postmarket",
        "feed_id": "mhra-news",
        "label": "MHRA News",
        "name": "MHRA news & alerts",
        "url": "https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
    },
    # --- Health Canada ---
    {
        "agency": "HC",
        "region": "Canada",
        "category": "recall",
        "feed_id": "hc-recalls",
        "label": "Health Canada Recalls",
        "name": "Health Canada — Medical devices recalls & alerts",
        "url": "https://recalls-rappels.canada.ca/en/feed/medical-devices-alerts-recalls",
    },
    # --- TGA (Australia) ---
    {
        "agency": "TGA",
        "region": "Australia",
        "category": "postmarket",
        "feed_id": "tga-news",
        "label": "TGA Safety Alerts",
        "name": "TGA safety alerts",
        "url": "https://tga.gov.au/feeds/alert/safety-alerts.xml",
    },
    # --- European Commission — public health ---
    {
        "agency": "EMA",
        "region": "European Union",
        "category": "guidance",
        "feed_id": "eu-health",
        "label": "EU Commission Health",
        "name": "European Commission — Health news",
        "url": "https://health.ec.europa.eu/rss_en",
    },
]

# How many items per feed to pull on each run (most recent)
MAX_ITEMS_PER_FEED = 25

# Archive cutoff: drop items older than this many days.
ARCHIVE_DAYS = 180  # ~6 months

# ---------------------------------------------------------------------------


def fetch_raw_feed(url: str, timeout: int = 30) -> bytes:
    """
    Fetch a feed URL using urllib so we control headers and decoding.
    Handles gzip and BOM. Raises on HTTP errors.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        if raw[:3] == b"\xef\xbb\xbf":  # strip UTF-8 BOM
            raw = raw[3:]
        return raw.lstrip()


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


def fetch_one(feed_cfg: dict) -> dict:
    """
    Fetch a single feed.
    Returns {"items": [...], "status": "ok"|"empty"|"error", "reason": str|None}.
    """
    print(f"  → {feed_cfg['agency']:6s} {feed_cfg['name']}", flush=True)
    try:
        raw = fetch_raw_feed(feed_cfg["url"])

        # If the server returned an HTML page (e.g. a 200 OK error page),
        # feedparser would fail with a confusing XML error. Catch it explicitly.
        head = raw[:200].decode("utf-8", errors="replace").lower()
        if "<html" in head or "<!doctype html" in head:
            reason = "server returned HTML, not a feed (URL may have moved)"
            print(f"    ✗ {reason}", flush=True)
            return {"items": [], "status": "error", "reason": reason}

        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            reason = f"feed parse error: {parsed.bozo_exception}"
            print(f"    ✗ {reason}", flush=True)
            return {"items": [], "status": "error", "reason": reason[:200]}

        items = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", entry.get("description", "")))
            link = entry.get("link", "")

            items.append({
                "title": title,
                "summary": truncate(summary, 500),
                "link": link,
                "published": parse_date(entry),
                "agency": feed_cfg["agency"],
                "region": feed_cfg["region"],
                "category": feed_cfg["category"],
                "feed_id": feed_cfg["feed_id"],
                "feed_label": feed_cfg["label"],
                "source": feed_cfg["name"],
            })

        if not items:
            print(f"    ⚠ feed returned 0 entries", flush=True)
            return {"items": [], "status": "empty", "reason": "feed returned 0 entries"}

        print(f"    ✓ {len(items)} item(s)", flush=True)
        return {"items": items, "status": "ok", "reason": None}

    except urllib.error.HTTPError as e:
        reason = f"HTTP {e.code}: {e.reason}"
        print(f"    ✗ {reason} (URL may have moved)", flush=True)
        return {"items": [], "status": "error", "reason": reason}
    except Exception as e:
        reason = str(e)[:200]
        print(f"    ✗ exception: {reason}", flush=True)
        return {"items": [], "status": "error", "reason": reason}


def main():
    print(f"Fetching {len(FEEDS)} feed(s)…", flush=True)
    new_items = []
    failed_feeds = []
    empty_feeds = []
    for cfg in FEEDS:
        result = fetch_one(cfg)
        new_items.extend(result["items"])
        if result["status"] == "error":
            failed_feeds.append({"feed_id": cfg["feed_id"], "name": cfg["name"], "reason": result["reason"]})
        elif result["status"] == "empty":
            empty_feeds.append({"feed_id": cfg["feed_id"], "name": cfg["name"], "reason": result["reason"]})
        time.sleep(0.5)  # be polite

    out_path = Path(__file__).parent.parent / "data" / "feed.json"
    out_path.parent.mkdir(exist_ok=True)

    # ----- Load existing archive -----
    existing_items = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing_items = existing.get("items", [])
            print(f"\nLoaded {len(existing_items)} existing item(s) from feed.json", flush=True)
        except Exception as e:
            print(f"\n⚠ Could not read existing feed.json ({e}); starting fresh.", flush=True)

    # ----- One-time backfill: patch old items with feed_id / feed_label -----
    # Old archive entries (from earlier script versions) have a 'source' field
    # but no feed_id/feed_label. Map by source name to fill them in.
    source_to_feed = {cfg["name"]: (cfg["feed_id"], cfg["label"]) for cfg in FEEDS}
    backfilled = 0
    for item in existing_items:
        if not item.get("feed_id"):
            src = item.get("source", "")
            if src in source_to_feed:
                fid, label = source_to_feed[src]
                item["feed_id"] = fid
                item["feed_label"] = label
                backfilled += 1
    if backfilled:
        print(f"  Backfilled feed_id on {backfilled} archived item(s)", flush=True)

    # ----- Merge new items with existing archive, dedupe by link -----
    # New items take precedence so we pick up any updated summaries/titles.
    seen = {}

    def key(item):
        return (item.get("link") or item.get("title") or "").strip().lower()

    for item in new_items:
        k = key(item)
        if k:
            seen[k] = item
    for item in existing_items:
        k = key(item)
        if k and k not in seen:
            seen[k] = item

    existing_keys = {key(i) for i in existing_items if key(i)}
    new_count = sum(1 for k in seen if k not in existing_keys)

    all_items = list(seen.values())

    # ----- Drop items older than ARCHIVE_DAYS -----
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_DAYS)
    cutoff_iso = cutoff.isoformat()
    before = len(all_items)
    all_items = [i for i in all_items if (i.get("published") or "") >= cutoff_iso]
    expired = before - len(all_items)
    if expired:
        print(f"  Dropped {expired} item(s) older than {ARCHIVE_DAYS} days", flush=True)

    # Sort newest first
    all_items.sort(key=lambda x: x.get("published") or "", reverse=True)

    if not new_items and existing_items:
        print(
            f"\n⚠ All {len(FEEDS)} feeds returned 0 items this run. "
            f"Keeping {len(all_items)} archived item(s).",
            flush=True,
        )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive_days": ARCHIVE_DAYS,
        "item_count": len(all_items),
        "feed_count": len(FEEDS),
        "new_this_run": new_count,
        "expired_this_run": expired,
        "failed_feeds": failed_feeds,
        "empty_feeds": empty_feeds,
        "items": all_items,
    }

    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"\nWrote {len(all_items)} item(s) → {out_path} "
        f"({new_count} new, {expired} expired, "
        f"{len(failed_feeds)} feed(s) errored, "
        f"{len(empty_feeds)} empty)",
        flush=True,
    )


if __name__ == "__main__":
    main()
