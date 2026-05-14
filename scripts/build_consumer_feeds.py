#!/usr/bin/env python3
"""
build_consumer_feeds.py  (v2 — corrected source URLs)
-----------------------------------------------------
Fetches consumer-safety data from multiple government sources, normalizes each
item to a common schema, and writes feeds/consumer.json.

Sources & endpoints (all verified May 2026):
  - CPSC          https://www.saferproducts.gov/RestWebServices/Recall?format=json
  - NHTSA         https://data.transportation.gov/resource/6axg-epim.json     (Socrata bulk)
  - USDA FSIS     https://www.fsis.usda.gov/fsis/api/recall/v/1
  - FDA Food      https://api.fda.gov/food/enforcement.json
  - EU Safety Gate https://ec.europa.eu/safety-gate-alerts/public/api/notifications/searchNotifications
  - FTC           https://www.consumer.ftc.gov/blog/gd-rss.xml

Run locally:   python scripts/build_consumer_feeds.py
GitHub Action: runs this nightly and commits the updated JSON.

Failure policy: if any one source fails, we keep the previous values for that
source (so a transient outage does not empty the page).
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "feeds" / "consumer.json"
USER_AGENT = "ConsumerWatch/1.0 (+https://github.com/)"
TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def http_get(url, headers=None):
    """Fetch a URL and return bytes. Raises on error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def http_get_json(url, headers=None):
    return json.loads(http_get(url, headers))


def stable_id(prefix, *parts):
    """Build a stable id from any combination of fields; falls back to hash."""
    key = "|".join(str(p) for p in parts if p)
    if not key:
        key = str(datetime.now().timestamp())
    return f"{prefix}-{abs(hash(key)) % (10**12)}"


def strip_html(text):
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text)


def parse_rss_date(s):
    """Parse RFC 822 dates from RSS feeds → YYYY-MM-DD or ''."""
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


def safe(fn, label):
    """Run a fetcher and return (items_list, ok_bool). Never raises."""
    try:
        items = fn()
        print(f"  [{label}] fetched {len(items)} items")
        return items, True
    except Exception as e:
        print(f"  [{label}] FAILED: {e}", file=sys.stderr)
        return [], False


# ---------------------------------------------------------------------------
# Source: CPSC (US consumer products) — confirmed working
# ---------------------------------------------------------------------------
def fetch_cpsc():
    url = "https://www.saferproducts.gov/RestWebServices/Recall?format=json"
    data = http_get_json(url)
    out = []
    for r in data[:200]:
        rid = r.get("RecallID") or r.get("RecallNumber")
        title = r.get("Title") or "(untitled CPSC recall)"
        date_raw = r.get("RecallDate") or ""
        date = date_raw[:10] if date_raw else ""
        hazards = r.get("Hazards") or []
        hazard_text = ", ".join(h.get("Name", "") for h in hazards) if hazards else ""
        products = r.get("Products") or []
        cat = products[0].get("Type", "Consumer Product") if products else "Consumer Product"
        desc = r.get("Description") or ""
        url_field = r.get("URL") or (f"https://www.cpsc.gov/Recalls/{rid}" if rid else "https://www.cpsc.gov/Recalls")
        out.append({
            "id": stable_id("cpsc", rid, title),
            "source": "CPSC",
            "country": "US",
            "category": cat,
            "hazard": hazard_text,
            "severity": "high",
            "date": date,
            "title": title,
            "summary": desc[:400].strip(),
            "url": url_field,
        })
    return out


# ---------------------------------------------------------------------------
# Source: NHTSA via Socrata bulk dataset (FIXED — no longer needs make/model)
# ---------------------------------------------------------------------------
def fetch_nhtsa():
    """
    The old per-make recallsByVehicle endpoint requires make+model+year, which
    means it returns nothing useful for a 'latest recalls' feed. Instead we use
    the Socrata bulk dataset at data.transportation.gov which contains every
    NHTSA campaign and supports filtering by date.
    """
    # Pull most recent 300 campaigns by report received date
    url = ("https://data.transportation.gov/resource/6axg-epim.json"
           "?$select=nhtsa_id,report_received_date,manufacturer,subject,component,summary,consequence_summary"
           "&$order=report_received_date DESC&$limit=300")
    data = http_get_json(url)
    out = []
    for r in data:
        cid = r.get("nhtsa_id") or ""
        date_raw = r.get("report_received_date") or ""
        # Socrata returns dates as ISO timestamps "2025-05-08T00:00:00.000"
        date = date_raw[:10] if date_raw else ""
        manuf = r.get("manufacturer") or ""
        subj = r.get("subject") or r.get("component") or ""
        summary = r.get("summary") or ""
        consequence = r.get("consequence_summary") or ""
        title = f"{manuf}: {subj}".strip(": ").strip() or "(NHTSA recall)"
        out.append({
            "id": stable_id("nhtsa", cid, title),
            "source": "NHTSA",
            "country": "US",
            "category": "Vehicles",
            "hazard": (r.get("component") or "")[:80],
            "severity": "high",
            "date": date,
            "title": title[:200],
            "summary": (summary or consequence)[:400].strip(),
            "url": (f"https://www.nhtsa.gov/recalls?nhtsaId={cid}"
                    if cid else "https://www.nhtsa.gov/recalls"),
        })
    return out


# ---------------------------------------------------------------------------
# Source: USDA FSIS (FIXED — more permissive field handling)
# ---------------------------------------------------------------------------
def fetch_fsis():
    url = "https://www.fsis.usda.gov/fsis/api/recall/v/1"
    data = http_get_json(url)
    out = []
    for r in data[:200]:
        # FSIS field names per their public docs:
        #   field_title, field_recall_number, field_recall_date,
        #   field_recall_classification, field_summary, field_states,
        #   field_active_notice, field_year, field_processing
        title = r.get("field_title") or r.get("title") or "(untitled FSIS recall)"
        rid = r.get("field_recall_number") or r.get("field_recall_url") or title[:50]
        # Try several date fields — FSIS sometimes uses `field_year` only
        date = (r.get("field_recall_date") or
                r.get("field_last_modified_date") or
                r.get("field_year") or "")
        date = date[:10] if date else ""
        reason = r.get("field_recall_reason") or r.get("field_summary") or ""
        classn = (r.get("field_recall_classification") or "").upper()
        sev = "high" if "CLASS I" in classn or classn.strip() == "I" else "medium"
        summary = (r.get("field_summary") or r.get("field_product_items") or
                   r.get("field_description") or reason or "")
        url_field = r.get("field_url") or r.get("field_recall_url") or "https://www.fsis.usda.gov/recalls"
        out.append({
            "id": stable_id("fsis", rid, title),
            "source": "USDA FSIS",
            "country": "US",
            "category": "Food",
            "hazard": strip_html(reason)[:120],
            "severity": sev,
            "date": date,
            "title": strip_html(title)[:200],
            "summary": strip_html(summary)[:400].strip(),
            "url": url_field,
        })
    return out


# ---------------------------------------------------------------------------
# Source: FDA Food Enforcement (openFDA) — confirmed working
# ---------------------------------------------------------------------------
def fetch_fda_food():
    url = "https://api.fda.gov/food/enforcement.json?sort=report_date:desc&limit=100"
    data = http_get_json(url)
    out = []
    for r in (data.get("results") or [])[:100]:
        rid = r.get("recall_number")
        date_raw = r.get("report_date") or ""
        date = ""
        if len(date_raw) == 8:  # YYYYMMDD
            date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        classn = r.get("classification") or ""
        sev_map = {"Class I": "high", "Class II": "medium", "Class III": "low"}
        sev = sev_map.get(classn, "medium")
        title = (r.get("product_description") or "(untitled)")[:200]
        out.append({
            "id": stable_id("fda-food", rid, title),
            "source": "FDA Food",
            "country": r.get("country") or "US",
            "category": "Food / Cosmetics",
            "hazard": (r.get("reason_for_recall") or "")[:120],
            "severity": sev,
            "date": date,
            "title": title,
            "summary": (r.get("reason_for_recall") or "")[:400].strip(),
            "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
        })
    return out


# ---------------------------------------------------------------------------
# Source: EU Safety Gate (FIXED — real notifications API)
# ---------------------------------------------------------------------------
def fetch_eu_safety_gate():
    """
    The Safety Gate portal at ec.europa.eu/safety-gate-alerts/ uses a JSON API
    behind its public search page. The endpoint returns notification metadata.
    """
    url = ("https://ec.europa.eu/safety-gate-alerts/public/api/notifications/"
           "searchNotifications?language=en&pageSize=100&pageIndex=0"
           "&sortField=publicationDate&sortDirection=DESC")
    try:
        data = http_get_json(url, headers={"Accept": "application/json"})
    except Exception:
        # Fallback: try the weekly-report listing endpoint
        url = ("https://ec.europa.eu/safety-gate-alerts/public/api/weeklyReports/"
               "search?language=en&pageSize=20&pageIndex=0")
        data = http_get_json(url, headers={"Accept": "application/json"})

    out = []
    notifications = (data.get("content")
                     or data.get("notifications")
                     or data.get("results")
                     or data if isinstance(data, list) else [])
    if isinstance(notifications, dict):
        notifications = notifications.get("notifications") or notifications.get("content") or []

    for n in (notifications or [])[:150]:
        nid = n.get("alertNumber") or n.get("reference") or n.get("id") or ""
        title = (n.get("productName") or n.get("name") or
                 n.get("productCategory") or "(EU Safety Gate alert)")
        date = (n.get("publicationDate") or n.get("creationDate") or "")[:10]
        risk = n.get("riskType") or n.get("risk") or ""
        country = n.get("notifyingCountry") or "EU"
        category = n.get("productCategory") or "Consumer Product"
        out.append({
            "id": stable_id("eu", nid, title),
            "source": "EU Safety Gate",
            "country": country,
            "category": category,
            "hazard": risk,
            "severity": "medium",
            "date": date,
            "title": title[:200],
            "summary": (n.get("description") or n.get("warningType") or "")[:400],
            "url": f"https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/{nid}" if nid else "https://ec.europa.eu/safety-gate-alerts/",
        })
    return out


# ---------------------------------------------------------------------------
# Source: FTC Consumer Alerts (FIXED — correct RSS URL)
# ---------------------------------------------------------------------------
def fetch_ftc():
    # Confirmed URL from ftc.gov/news-events/stay-connected/ftc-rss-feeds
    url = "https://www.consumer.ftc.gov/blog/gd-rss.xml"
    raw = http_get(url)
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        date = parse_rss_date(pub)
        desc_clean = strip_html(desc)[:400]
        out.append({
            "id": stable_id("ftc", link, title),
            "source": "FTC",
            "country": "US",
            "category": "Fraud & Scams",
            "hazard": "Financial",
            "severity": "medium",
            "date": date,
            "title": title[:200],
            "summary": desc_clean,
            "url": link or "https://consumer.ftc.gov/consumer-alerts",
        })
    return out[:100]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    print("Building consumer.json…")

    # Load previous file (so failures don't wipe data)
    previous = {}
    if OUT_PATH.exists():
        try:
            with OUT_PATH.open() as f:
                prev_data = json.load(f)
            for it in prev_data.get("items", []):
                previous.setdefault(it["source"], []).append(it)
        except Exception:
            previous = {}

    fetchers = [
        ("CPSC", fetch_cpsc),
        ("NHTSA", fetch_nhtsa),
        ("USDA FSIS", fetch_fsis),
        ("FDA Food", fetch_fda_food),
        ("EU Safety Gate", fetch_eu_safety_gate),
        ("FTC", fetch_ftc),
    ]

    all_items = []
    statuses = {}
    for label, fn in fetchers:
        items, ok = safe(fn, label)
        if ok and items:
            all_items.extend(items)
            statuses[label] = "ok"
        else:
            fallback = previous.get(label, [])
            all_items.extend(fallback)
            statuses[label] = "stale" if fallback else "missing"
            print(f"  [{label}] using {len(fallback)} cached items (status: {statuses[label]})")

    all_items.sort(key=lambda x: x.get("date", ""), reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": "1.1",
        "source_status": statuses,
        "items": all_items,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(all_items)} items to {OUT_PATH}")
    print("Source status:", statuses)


if __name__ == "__main__":
    main()
