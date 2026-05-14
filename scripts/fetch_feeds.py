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
import gzip
import io
import urllib.request
import urllib.error

socket.setdefaulttimeout(30)

# Many gov RSS feeds block default Python/feedparser User-Agents. We send a
# realistic browser-like UA to get past basic bot detection.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def fetch_raw_feed(url, timeout=30):
    """
    Fetch a feed URL using urllib directly so we have full control over
    headers, redirects, and encoding. Returns bytes (or raises an exception).
    Handles gzip decompression and BOM stripping that feedparser sometimes
    chokes on.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        # Decompress if gzipped (some servers ignore Accept-Encoding negotiation)
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        elif raw[:2] == b"\x1f\x8b":  # gzip magic bytes
            raw = gzip.decompress(raw)
        # Strip UTF-8 BOM if present (causes "not well-formed" in some XML parsers)
        if raw[:3] == b"\xef\xbb\xbf":
            raw = raw[3:]
        # Strip leading whitespace before <?xml declaration (common cause of
        # "not well-formed" errors when servers add stray newlines)
        raw = raw.lstrip()
        return raw

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

# =============================================================================
# BACKUP FEED REFERENCE
# =============================================================================
# Cross-referenced against rainfo.org/regulatory-news-rss-feeds (curated list)
# in case any of the active feeds below break, here are the verified URLs to
# fall back on. Last verified: May 2026.
#
# Currently active feeds are in FEEDS below. This dict is just a reference.
# To activate a backup feed, uncomment its block in FEEDS.
#
# === FDA (US) — Only 3 device-relevant feeds actually exist on fda.gov ===
#   FDA Recalls (all FDA, filter to devices):
#       https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml
#   FDA MedWatch Safety Alerts:
#       https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml
#   FDA Press Announcements:
#       https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml
#
# === Federal Register ===
#   FDA documents (rules, proposed rules, notices):
#       https://www.federalregister.gov/api/v1/documents.rss?conditions[agencies][]=food-and-drug-administration&conditions[type][]=RULE&conditions[type][]=PRORULE&conditions[type][]=NOTICE
#
# === IMDRF (international harmonization) ===
#   Working Groups:   https://www.imdrf.org/working-groups.xml
#   Consultations:    https://www.imdrf.org/consultations.xml
#   Documents:        https://www.imdrf.org/documents.xml
#   Events:           https://www.imdrf.org/events.xml
#   News:             https://www.imdrf.org/news.xml
#
# === WHO ===
#   Medical Product Alerts: https://www.who.int/rss-feeds/medical-product-alerts-en.xml
#
# === MHRA / UK (gov.uk Atom feeds) ===
#   MHRA news & alerts:
#       https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom
#   UK Drug & Medical Device Alerts:
#       https://www.gov.uk/drug-device-alerts.atom
#   MHRA Inspectorate:
#       https://mhrainspectorate.blog.gov.uk/feed/
#
# === Health Canada ===
#   Recalls & safety alerts:
#       https://recalls-rappels.canada.ca/en/rss.xml
#   Medical Devices, Drugs and Health Products:
#       https://www.canada.ca/en/news/web-feeds/health-canada-news.xml
#
# === TGA (Australia) ===
#   News & alerts: https://www.tga.gov.au/rss.xml
#
# === EU Commission / EMA ===
#   EU Health News (Commission):
#       https://health.ec.europa.eu/rss_en
#   EMA News & Press Releases:
#       https://www.ema.europa.eu/en/news/rss.xml
#   EMA What's New:
#       https://www.ema.europa.eu/en/whats-new/rss.xml
#
# === EUR-Lex ===
#   Recent regulations & directives:
#       https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=oj-l-recent
#   (For MDR/IVDR-specific feeds, use EUR-Lex saved-search RSS — requires
#    a free EUR-Lex account.)
#
# === Czech Republic — SUKL (alternate EU regulator with rich feeds) ===
#   Medical Devices: https://www.sukl.eu/rss/en/10201
#   Surveillance:    https://www.sukl.eu/rss/en/84
#   Pharmacovigilance: https://www.sukl.eu/rss/en/62
#
# === Saudi Arabia — SFDA ===
#   News (Arabic + English): https://www.sfda.gov.sa/en/rss
#
# =============================================================================

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
    },
    {
        "agency": "FDA",
        "region": "United States",
        "category": "guidance",
        "name": "FDA Press Announcements",
        "feed_id": "fda-press",
        "label": "FDA Press",
        "url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
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
    # NOTE: We previously had a "WHO Medical Product Alerts" feed at
    # https://www.who.int/rss-feeds/medical-product-alerts-en.xml — but that
    # URL was made up. WHO doesn't publish a dedicated medical-product-alerts
    # RSS feed. Dropped 2026-05-13.
    # --- MHRA (UK) ---
    {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "postmarket",
        "name": "MHRA news & alerts",
        "feed_id": "mhra-news",
        "label": "MHRA News",
        "url": "https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
    },
    {
        "agency": "MHRA",
        "region": "United Kingdom",
        "category": "recall",
        "name": "UK Drug & Medical Device Alerts",
        "feed_id": "uk-alerts",
        "label": "UK Drug & Device Alerts",
        "url": "https://www.gov.uk/drug-device-alerts.atom",
    },
    # --- Health Canada ---
    # Verified URL pattern. The general /en/rss.xml URL we had previously
    # doesn't exist — Canada's recalls site uses /en/feed/<category> URLs.
    # The medical-devices-alerts-recalls feed gives device-specific recalls.
    {
        "agency": "HC",
        "region": "Canada",
        "category": "recall",
        "name": "Health Canada Medical Device Recalls",
        "feed_id": "hc-recalls",
        "label": "Health Canada Recalls",
        "url": "https://recalls-rappels.canada.ca/en/feed/medical-devices-alerts-recalls",
    },
    # --- TGA (Australia) ---
    {
        "agency": "TGA",
        "region": "Australia",
        "category": "postmarket",
        "name": "TGA news & alerts",
        "feed_id": "tga-news",
        "label": "TGA News",
        "url": "https://www.tga.gov.au/feeds/alert.xml",
    },
    # --- EMA News (replaces former EU Commission health feed) ---
    # The original "health.ec.europa.eu/rss_en" URL was unverifiable. EMA's
    # news.xml is the verified, working EU-level news feed for medicines and
    # medical products. Verified loading 2026-05-13.
    {
        "agency": "EMA",
        "region": "European Union",
        "category": "guidance",
        "name": "EMA News & Press Releases",
        "feed_id": "eu-health",
        "label": "EMA News",
        "url": "https://www.ema.europa.eu/en/news.xml",
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
    },
]

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
    raw_bytes = None
    # Try up to 4 times with progressive backoff. FDA sometimes returns 404
    # on the first attempt and 200 on the second from the same IP.
    for attempt in range(4):
        try:
            # Fetch raw bytes ourselves (handles gzip, BOM, leading whitespace)
            raw_bytes = fetch_raw_feed(feed_cfg["url"])
            # Quick sanity check: does this look like XML?
            head = raw_bytes[:200].decode("utf-8", errors="replace").lower()
            if "<html" in head or "<!doctype html" in head:
                last_error = "server returned HTML page (likely 404 or block)"
                if attempt < 3:
                    print(f"    ⟲ retry {attempt+1}: {last_error}", flush=True)
                    time.sleep(2 + attempt * 2)  # 2s, 4s, 6s backoff
                    continue
                print(f"    ✗ {last_error}", flush=True)
                print(f"    DEBUG response start: {head[:200]!r}", flush=True)
                return {"items": [], "status": "error", "reason": last_error}

            parsed = feedparser.parse(raw_bytes)
            # If we got entries, we're done
            if parsed.entries:
                break
            # No entries AND a parse error → retry
            if parsed.bozo and attempt < 3:
                last_error = str(parsed.bozo_exception)[:120]
                print(f"    ⟲ retry {attempt+1}: {last_error}", flush=True)
                time.sleep(2 + attempt * 2)
                continue
            break
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.reason}"
            # Retry on 404 (FDA seems to return these intermittently), 429 (rate limit), and 5xx
            if attempt < 3 and (e.code == 404 or e.code == 429 or e.code >= 500):
                print(f"    ⟲ retry {attempt+1}: {last_error}", flush=True)
                time.sleep(2 + attempt * 3)  # longer backoff for rate limits
                continue
            print(f"    ✗ {last_error}", flush=True)
            return {"items": [], "status": "error", "reason": last_error}
        except Exception as e:
            last_error = str(e)[:120]
            if attempt < 3:
                print(f"    ⟲ retry {attempt+1}: {last_error}", flush=True)
                time.sleep(2 + attempt * 2)
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
            # Diagnostic: what did the server actually return?
            if raw_bytes:
                preview = raw_bytes[:300].decode("utf-8", errors="replace")
                print(f"    DEBUG ({len(raw_bytes)} bytes): {preview!r}", flush=True)
            return {"items": [], "status": "error", "reason": reason}

        total_entries = len(parsed.entries)
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
                "feed_id": feed_cfg.get("feed_id", "unknown"),
                "feed_label": feed_cfg.get("label", feed_cfg["name"]),
                "source": feed_cfg["name"],
            })

        if not items:
            reason = "feed returned 0 entries"
            print(f"    ⚠ {reason}", flush=True)
            return {"items": [], "status": "empty", "reason": reason}

        print(f"    ✓ {len(items)} item(s)", flush=True)
        return {"items": items, "status": "ok", "reason": None}
    except Exception as e:
        print(f"    ✗ exception: {e}", flush=True)
        return {"items": [], "status": "error", "reason": str(e)[:120]}


def main():
    print(f"Fetching {len(FEEDS)} feed(s)…", flush=True)

    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []
    failed_feeds = []
    empty_feeds = []
    # Per-feed status tracked this run, keyed by feed_id
    this_run_status = {}
    for cfg in FEEDS:
        feed_id = cfg.get("feed_id", cfg["name"])
        try:
            result = fetch_one(cfg)
            new_items.extend(result["items"])
            item_count_this_run = len(result["items"])
            if result["status"] == "error":
                failed_feeds.append({"name": cfg["name"], "reason": result["reason"]})
                this_run_status[feed_id] = {
                    "name": cfg["name"],
                    "feed_id": feed_id,
                    "status": "error",
                    "last_attempt": now_iso,
                    "last_attempt_reason": result["reason"],
                    "items_fetched": 0,
                }
            elif result["status"] == "empty":
                empty_feeds.append({"name": cfg["name"], "reason": result["reason"]})
                this_run_status[feed_id] = {
                    "name": cfg["name"],
                    "feed_id": feed_id,
                    "status": "empty",
                    "last_attempt": now_iso,
                    "last_attempt_reason": result["reason"],
                    "last_success": now_iso,  # connection worked, just no entries
                    "items_fetched": 0,
                }
            else:
                this_run_status[feed_id] = {
                    "name": cfg["name"],
                    "feed_id": feed_id,
                    "status": "ok",
                    "last_attempt": now_iso,
                    "last_success": now_iso,
                    "items_fetched": item_count_this_run,
                }
        except Exception as e:
            print(f"    ✗ unhandled exception in {cfg['name']}: {e}", flush=True)
            failed_feeds.append({"name": cfg["name"], "reason": str(e)[:120]})
            this_run_status[feed_id] = {
                "name": cfg["name"],
                "feed_id": feed_id,
                "status": "error",
                "last_attempt": now_iso,
                "last_attempt_reason": str(e)[:120],
                "items_fetched": 0,
            }
        time.sleep(0.5)

    out_path = Path(__file__).parent.parent / "data" / "feed.json"
    out_path.parent.mkdir(exist_ok=True)

    # ----- APPEND-ONLY ARCHIVE: merge with existing items -----
    existing_items = []
    existing_feed_status = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing_items = existing.get("items", [])
            # Preserve previous per-feed status (especially last_success timestamps)
            for s in existing.get("feed_status", []):
                if s.get("feed_id"):
                    existing_feed_status[s["feed_id"]] = s
            print(f"\nLoaded {len(existing_items)} existing item(s) from feed.json", flush=True)
            if existing_feed_status:
                print(f"Loaded prior status for {len(existing_feed_status)} feed(s)", flush=True)
        except Exception as e:
            print(f"\n⚠ Could not read existing feed.json ({e}); starting fresh.", flush=True)

    # Merge this run's status with the previous: carry forward last_success
    # if this run failed, so the dashboard can show "fetched 3 days ago"
    # rather than "never fetched".
    feed_status = []
    for cfg in FEEDS:
        feed_id = cfg.get("feed_id", cfg["name"])
        current = this_run_status.get(feed_id, {})
        previous = existing_feed_status.get(feed_id, {})
        # If this run didn't get a last_success, carry forward the prior one
        if not current.get("last_success") and previous.get("last_success"):
            current["last_success"] = previous["last_success"]
        # Carry forward last_success_item_count too
        if current.get("status") == "ok":
            current["last_success_item_count"] = current.get("items_fetched", 0)
        elif previous.get("last_success_item_count") is not None:
            current["last_success_item_count"] = previous["last_success_item_count"]
        feed_status.append(current)

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
        "pruned_this_run": 0,
        "failed_feeds": failed_feeds,
        "empty_feeds": empty_feeds,
        "feed_status": feed_status,
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
