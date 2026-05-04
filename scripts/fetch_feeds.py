#!/usr/bin/env python3
"""
fetch_feeds.py — fetches RSS/Atom feeds from regulatory authorities
and writes a consolidated feed.json that the dashboard reads.

Run by GitHub Actions on a schedule. Free, no API keys needed.
"""

import json
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser  # pip install feedparser

# Hard timeout for any single network request (seconds).
# Without this, a slow feed can hang the entire workflow.
socket.setdefaulttimeout(30)

# Many gov RSS feeds block default Python/feedparser User-Agents. We send a
# realistic browser-like UA to get past basic bot detection.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 RegulatoryWatchBot/1.0"
)

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
    # ============================================================
    # US FDA — Only these RSS feeds actually exist on fda.gov.
    # The /medical-device-recalls/, /medical-device-safety/, and
    # /medical-devices/ URLs commonly cited online return 404 HTML pages
    # (which feedparser can't parse, hence "mismatched tag" errors).
    # The general feeds below cover all FDA topics; we filter to devices.
    # Source: https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds
    # ============================================================
    {
        "agency": "FDA",
        "region": "United States",
        "category": "recall",
        "name": "FDA Recalls",
        "feed_id": "fda-recalls",
        "label": "FDA Recalls",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml",
        "filter_to_devices": True,
    },
    # --- US FDA: broader feeds (filtered to device topics) ---
    {
        "agency": "FDA",
        "region": "United States",
        "category": "recall",
        "name": "FDA MedWatch Safety Alerts",
        "feed_id": "fda-medwatch",
        "label": "FDA MedWatch",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml",
        "filter_to_devices": True,
    },
    {
        "agency": "FDA",
        "region": "United States",
        "category": "guidance",
        "name": "FDA Press Announcements",
        "feed_id": "fda-press",
        "label": "FDA Press",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
        "filter_to_devices": True,
    },
    # --- Federal Register (pre-publication / proposed rules) ---
    {
        "agency": "FR",
        "region": "United States",
        "category": "prepub",
        "name": "Federal Register — FDA documents",
        "feed_id": "federal-register",
        "label": "Federal Register",
        "url": "https://www.federalregister.gov/api/v1/documents.rss?conditions[agencies][]=food-and-drug-administration&conditions[type][]=RULE&conditions[type][]=PRORULE&conditions[type][]=NOTICE",
        "filter_to_devices": True,
    },
    # --- IMDRF: international harmonization ---
    {
        "agency": "IMDRF",
        "region": "International",
        "category": "guidance",
        "name": "IMDRF Working Groups",
        "feed_id": "imdrf-wg",
        "label": "IMDRF Working Groups",
        "url": "https://www.imdrf.org/working-groups.xml",
    },
    {
        "agency": "IMDRF",
        "region": "International",
        "category": "guidance",
        "name": "IMDRF Consultations",
        "feed_id": "imdrf-consult",
        "label": "IMDRF Consultations",
        "url": "https://www.imdrf.org/consultations.xml",
    },
    # --- WHO: international safety alerts ---
    {
        "agency": "WHO",
        "region": "International",
        "category": "recall",
        "name": "WHO Medical Product Alerts",
        "feed_id": "who-alerts",
        "label": "WHO Alerts",
        "url": "https://www.who.int/rss-feeds/medical-product-alerts-en.xml",
        "filter_to_devices": True,
    },
    # --- MHRA (UK) ---
    {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "postmarket",
        "name": "MHRA news & alerts",
        "feed_id": "mhra-news",
        "label": "MHRA News",
        "url": "https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
        "filter_to_devices": True,
    },
    {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "recall",
        "name": "UK Drug & Medical Device Alerts",
        "feed_id": "uk-alerts",
        "label": "UK Drug & Device Alerts",
        "url": "https://www.gov.uk/drug-device-alerts.atom",
        "filter_to_devices": True,
    },
    # --- Health Canada ---
    {
        "agency": "HC",
        "region": "Canada",
        "category": "recall",
        "name": "Health Canada recalls & safety alerts",
        "feed_id": "hc-recalls",
        "label": "Health Canada Recalls",
        "url": "https://recalls-rappels.canada.ca/en/rss.xml",
        "filter_to_devices": True,
    },
    # --- TGA (Australia) ---
    {
        "agency": "TGA",
        "region": "Australia",
        "category": "postmarket",
        "name": "TGA news & alerts",
        "feed_id": "tga-news",
        "label": "TGA News",
        "url": "https://www.tga.gov.au/rss.xml",
        "filter_to_devices": True,
    },
    # --- European Commission — public health ---
    {
        "agency": "EMA",
        "region": "European Union",
        "category": "guidance",
        "name": "European Commission — Health news",
        "feed_id": "eu-health",
        "label": "EU Commission Health",
        "url": "https://health.ec.europa.eu/rss_en",
        "filter_to_devices": True,
    },
    # --- EU OJEU L series (legal acts) ---
    {
        "agency": "EU",
        "region": "European Union",
        "category": "prepub",
        "name": "EUR-Lex — Recent regulations & directives",
        "feed_id": "eur-lex",
        "label": "EUR-Lex",
        "url": "https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=oj-l-recent",
        "filter_to_devices": True,
    },
]

# Used to filter out drug-only or food-only items from broad feeds.
DEVICE_KEYWORDS = [
    "device", "devices", "510(k)", "510k", "pma", "de novo", "udi", "ivd",
    "diagnostic", "diagnostics", "implant", "implantable", "cdrh",
    "mdr", "ivdr", "mdcg", "premarket", "post-market", "postmarket",
    "vigilance", "psur", "estar", "notified body", "samd",
    "software as a medical", "ai/ml", "cybersecurity", "biocompatibility",
    "sterilization", "in vitro", "scanner", "pacemaker", "stent", "catheter",
    "surgical", "medtech", "medical equipment", "medical product",
    "infusion pump", "ventilator", "endoscope", "defibrillator",
    "patient monitor", "imaging", "mri", "ct scan", "ultrasound",
    "wearable", "digital health", "remote monitoring",
    "combination product", "device-led",
    # Recalls/alerts often use these without the word "device"
    "recall", "field action", "fsca", "fsn", "safety communication",
    "safety alert", "safety notice", "safety information",
]

# Items containing ANY of these are dropped — they're drugs/biologics/food
# and not relevant even if they happen to contain a device keyword.
DRUG_ONLY_KEYWORDS = [
    "vaccine", "vaccines", "immunization",
    "investigational new drug", " ind ",
    "biologics license", "biologic application",
    "drug shortage", "drug application", "drug approval",
    "approves new drug", "approves first-in-class", "approves treatment for",
    "tablet", "capsule", "injection of", "infusion of",
    "pediatric drug", "drug-resistant",
    "tobacco", "cigar", "vaping", "e-cigarette", "nicotine",
    "food safety", "foodborne", "infant formula",
    "dietary supplement",
    "veterinary", "animal drug", "animal health",
    "cosmetic", "cosmetics",
]


def is_device_related(text: str) -> bool:
    """
    Returns True if the text is plausibly medical-device related.

    Logic: We KEEP items by default. We only REJECT if the item is clearly
    about something else (drugs, food, tobacco, vet products) AND lacks any
    device signal. This avoids false negatives — broad RSS feeds frequently
    publish device items with non-obvious wording (e.g. "infusion sets",
    "field safety notice", "manufacturer reporting") that don't trigger a
    rigid keyword list.
    """
    t = (text or "").lower()
    if not t.strip():
        return False

    # HARD OVERRIDES — these are never devices, drop regardless of other signals
    HARD_DROPS = (
        "tobacco", "cigar", "vaping", "e-cigarette", "nicotine",
        "infant formula", "dietary supplement",
        "veterinary", "animal drug", "animal health",
        "cosmetic", "cosmetics", "foodborne", "food safety",
        "vaccine", "vaccines",
    )
    if any(k in t for k in HARD_DROPS):
        # Allow "combination product" mentions through (rare drug-device combos)
        if "combination product" in t or "device-led combination" in t:
            return True
        return False

    has_device_signal = any(k in t for k in DEVICE_KEYWORDS)
    has_drug_signal   = any(k in t for k in DRUG_ONLY_KEYWORDS)

    # Combination products always pass
    if "combination product" in t or "device-led combination" in t:
        return True

    # Drug signal AND no device signal → reject
    if has_drug_signal and not has_device_signal:
        return False

    # Drug signal WITH device signal → keep (e.g. drug-eluting stent)
    if has_drug_signal and has_device_signal:
        return True

    # No drug signal → keep regardless. Device-specific feeds (FDA recalls,
    # MHRA alerts) already only publish device items, so we trust them.
    return True

# How many items per feed (most recent)
MAX_ITEMS_PER_FEED = 25

# Total items in the consolidated archive (oldest beyond this are dropped)
MAX_TOTAL_ITEMS = 1000

# ---------------------------------------------------------------------------


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
    Returns dict with: items (list), status ('ok'|'error'|'empty'), reason (str|None).
    """
    print(f"  → {feed_cfg['agency']:6s} {feed_cfg['name']}", flush=True)
    items = []

    # Try up to 2 times; transient timeouts and 5xx are common on gov servers
    parsed = None
    last_error = None
    for attempt in range(2):
        try:
            parsed = feedparser.parse(
                feed_cfg["url"],
                agent=USER_AGENT,
                request_headers={
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # If we got entries, we're done
            if parsed.entries:
                break
            # No entries AND a parse error → maybe HTML was served, retry once
            if parsed.bozo and attempt == 0:
                last_error = str(parsed.bozo_exception)[:120]
                print(f"    ⟲ retry 1: {last_error}", flush=True)
                time.sleep(2)
                continue
            break
        except Exception as e:
            last_error = str(e)[:120]
            if attempt == 0:
                print(f"    ⟲ retry 1: {last_error}", flush=True)
                time.sleep(2)
                continue
            print(f"    ✗ exception after retry: {last_error}", flush=True)
            return {"items": [], "status": "error", "reason": last_error}

    try:
        if parsed is None:
            return {"items": [], "status": "error", "reason": last_error or "unknown error"}

        if parsed.bozo and not parsed.entries:
            reason = str(parsed.bozo_exception)[:120]
            # Distinguish between bot-blocking (HTML returned) and real format errors
            try:
                # parsed.feed.title is empty but parsed has 'feed' attr; feedparser
                # leaves the raw response in feed.summary if parsing failed badly.
                # Easier: just check if reason mentions tags / tokens (XML parse errors).
                if "tag" in reason.lower() or "token" in reason.lower() or "well-formed" in reason.lower():
                    reason = f"XML parse error (likely served HTML/blocked): {reason}"
            except Exception:
                pass
            print(f"    ✗ feed error: {reason}", flush=True)
            return {"items": [], "status": "error", "reason": reason}

        total_entries = len(parsed.entries)
        filter_devices = feed_cfg.get("filter_to_devices", False)
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", entry.get("description", "")))
            link = entry.get("link", "")
            full_text = f"{title} {summary}"

            # Apply device-keyword filter to feeds marked filter_to_devices=True
            if filter_devices and not is_device_related(full_text):
                continue

            items.append({
                "title": title,
                "summary": truncate(summary, 500),
                "link": link,
                "published": parse_date(entry),
                "agency": feed_cfg["agency"],
                "region": feed_cfg["region"],
                "category": feed_cfg["category"],
                "feed_id": feed_cfg.get("feed_id", "unknown"),
                "feed_label": feed_cfg.get("label", feed_cfg["name"]),
                "source": feed_cfg["name"],
            })

        if not items:
            reason = (f"0 device-related items (of {total_entries} total)"
                      if total_entries else "feed returned 0 entries")
            print(f"    ⚠ {reason}", flush=True)
            return {"items": [], "status": "empty", "reason": reason}

        print(f"    ✓ {len(items)} item(s)", flush=True)
        return {"items": items, "status": "ok", "reason": None}
    except Exception as e:
        print(f"    ✗ exception: {e}", flush=True)
        return {"items": [], "status": "error", "reason": str(e)[:120]}


def main():
    print(f"Fetching {len(FEEDS)} feed(s)…", flush=True)
    new_items = []
    failed_feeds = []
    empty_feeds = []
    for cfg in FEEDS:
        try:
            result = fetch_one(cfg)
            new_items.extend(result["items"])
            if result["status"] == "error":
                failed_feeds.append({"name": cfg["name"], "reason": result["reason"]})
            elif result["status"] == "empty":
                empty_feeds.append({"name": cfg["name"], "reason": result["reason"]})
        except Exception as e:
            print(f"    ✗ unhandled exception in {cfg['name']}: {e}", flush=True)
            failed_feeds.append({"name": cfg["name"], "reason": str(e)[:120]})
        time.sleep(0.5)

    out_path = Path(__file__).parent.parent / "data" / "feed.json"
    out_path.parent.mkdir(exist_ok=True)

    # ----- APPEND-ONLY ARCHIVE: merge with existing items -----
    existing_items = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing_items = existing.get("items", [])
            print(f"\nLoaded {len(existing_items)} existing item(s) from feed.json", flush=True)
        except Exception as e:
            print(f"\n⚠ Could not read existing feed.json ({e}); starting fresh.", flush=True)

    # Build lookup: source name → (feed_id, label) — used to backfill old items
    # that were written before feed_id existed.
    source_to_feed = {
        cfg["name"]: (cfg.get("feed_id", "unknown"), cfg.get("label", cfg["name"]))
        for cfg in FEEDS
    }
    backfilled = 0
    for item in existing_items:
        if not item.get("feed_id") or item.get("feed_id") == "unknown":
            src = item.get("source", "")
            if src in source_to_feed:
                fid, label = source_to_feed[src]
                item["feed_id"] = fid
                item["feed_label"] = label
                backfilled += 1
    if backfilled:
        print(f"  Backfilled feed_id on {backfilled} archived item(s)", flush=True)

    # Re-apply the device filter to existing archived items so old drug-related
    # entries (from earlier looser filters) get cleaned up. Uses the same
    # is_device_related() function as fetching to keep behavior consistent.
    pruned_count = 0
    pruned_items = []
    for item in existing_items:
        full_text = f"{item.get('title', '')} {item.get('summary', '')}"
        # Items from device-specific feeds (no filter_to_devices flag) were
        # never filtered originally; trust the upstream source and keep them.
        item_source = item.get("source", "")
        feed_was_filtered = False
        for cfg in FEEDS:
            if cfg["name"] == item_source:
                feed_was_filtered = cfg.get("filter_to_devices", False)
                break
        if feed_was_filtered:
            # Re-check against current rules
            if not is_device_related(full_text):
                pruned_count += 1
                continue
        pruned_items.append(item)
    existing_items = pruned_items
    if pruned_count:
        print(f"  Pruned {pruned_count} now-out-of-scope item(s) from archive", flush=True)

    # De-dupe by link (or by title if no link). New items take precedence
    # so we pick up any updated summaries/titles from the upstream feed.
    seen = {}
    def key(item):
        return (item.get("link") or item.get("title") or "").strip().lower()

    # Add NEW items first so they overwrite old versions of the same URL
    for item in new_items:
        k = key(item)
        if k:
            seen[k] = item
    # Then add existing items only if their key isn't already present
    new_count = 0
    for item in existing_items:
        k = key(item)
        if k and k not in seen:
            seen[k] = item
        elif k in seen and item not in new_items:
            # this URL was in the existing archive
            pass
    # Count truly new additions
    existing_keys = {key(i) for i in existing_items if key(i)}
    new_count = sum(1 for k in seen if k not in existing_keys)

    all_items = list(seen.values())

    # Sort newest first, cap total
    all_items.sort(key=lambda x: x.get("published") or "", reverse=True)
    if len(all_items) > MAX_TOTAL_ITEMS:
        all_items = all_items[:MAX_TOTAL_ITEMS]

    if not new_items and existing_items:
        print(
            f"\n⚠ All {len(FEEDS)} feeds returned 0 items this run. "
            f"Keeping {len(existing_items)} archived item(s).",
            flush=True,
        )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(all_items),
        "feed_count": len(FEEDS),
        "new_this_run": new_count,
        "pruned_this_run": pruned_count,
        "failed_feeds": failed_feeds,
        "empty_feeds": empty_feeds,
        "items": all_items,
    }

    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"\nWrote {len(all_items)} item(s) → {out_path} "
        f"({new_count} new, "
        f"{len(failed_feeds)} feed(s) errored, "
        f"{len(empty_feeds)} empty)",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never fail the workflow — just log and exit cleanly.
        print(f"FATAL but recoverable: {e}", flush=True)
        sys.exit(0)
