#!/usr/bin/env python3
"""
build_consumer_feeds.py
-----------------------
Fetches consumer-safety data from multiple government sources, normalizes each
item to a common schema, and writes feeds/consumer.json.

Sources:
  - CPSC          (US consumer products)        https://www.saferproducts.gov/RestWebServices/Recall
  - NHTSA         (US vehicle recalls)          https://api.nhtsa.gov/recalls/recallsByVehicle
  - USDA FSIS     (meat/poultry/egg)            https://www.fsis.usda.gov/fsis/api/recall/v/1
  - FDA Food      (food enforcement)            https://api.fda.gov/food/enforcement.json
  - EU Safety Gate                              https://ec.europa.eu/safety-gate-alerts/screen/webReport
  - FTC scams (RSS)                             https://consumer.ftc.gov/consumer-alerts/feed

Run locally:   python scripts/build_consumer_feeds.py
GitHub Action: runs this nightly and commits the updated JSON.

Failure policy: if any one source fails, we still write what we have and keep
the previous values for the failed source (so a transient outage does not
empty the page).
"""

import json
import os
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "feeds" / "consumer.json"
USER_AGENT = "ConsumerWatch/1.0 (+https://github.com/)"
TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Common schema
# ---------------------------------------------------------------------------
# Every item we emit looks like:
# {
#   "id":       str   stable unique id (source + native id)
#   "source":   str   "CPSC" | "NHTSA" | "USDA FSIS" | "FDA Food" | "EU Safety Gate" | "FTC"
#   "country":  str   "US" | "EU" | etc.
#   "category": str   "Children's Products" | "Vehicles" | "Food" | ...
#   "hazard":   str   "Choking" | "Fire" | "Contamination" | ...
#   "severity": str   "high" | "medium" | "low"
#   "date":     str   ISO date YYYY-MM-DD
#   "title":    str
#   "summary":  str
#   "url":      str   link to official source detail
# }


def http_get(url, headers=None):
    """Fetch a URL and return bytes. Raises on error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def http_get_json(url, headers=None):
    return json.loads(http_get(url, headers))


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
# Source: CPSC (US consumer products)
# ---------------------------------------------------------------------------
def fetch_cpsc():
    url = "https://www.saferproducts.gov/RestWebServices/Recall?format=json"
    data = http_get_json(url)
    out = []
    for r in data[:200]:  # cap per source
        rid = r.get("RecallID") or r.get("RecallNumber")
        if not rid:
            continue
        title = r.get("Title") or "(untitled CPSC recall)"
        # CPSC dates: "2024-03-21T00:00:00"
        date_raw = r.get("RecallDate") or ""
        date = date_raw[:10] if date_raw else ""
        hazards = r.get("Hazards") or []
        hazard_text = ", ".join(h.get("Name", "") for h in hazards) if hazards else ""
        products = r.get("Products") or []
        cat = products[0].get("Type", "Consumer Product") if products else "Consumer Product"
        desc = r.get("Description") or ""
        url_field = r.get("URL") or f"https://www.cpsc.gov/Recalls/{rid}"
        out.append({
            "id": f"cpsc-{rid}",
            "source": "CPSC",
            "country": "US",
            "category": cat,
            "hazard": hazard_text,
            "severity": "high",  # CPSC entries are recalls by definition
            "date": date,
            "title": title,
            "summary": desc[:400].strip(),
            "url": url_field,
        })
    return out


# ---------------------------------------------------------------------------
# Source: NHTSA (US vehicle recalls)
# ---------------------------------------------------------------------------
def fetch_nhtsa():
    """
    NHTSA's public API requires make/model/year params. To get a broad latest
    stream we use the recall campaigns CSV-like JSON endpoint that lists recent
    campaigns. As a simple, dependency-free approach we hit the per-make API
    for a handful of high-volume manufacturers and merge results.
    """
    makes = ["FORD", "TOYOTA", "HONDA", "TESLA", "GM", "STELLANTIS", "HYUNDAI"]
    out = []
    current_year = datetime.now().year
    for make in makes:
        for year_offset in (0, 1):  # current year + previous year
            year = current_year - year_offset
            url = f"https://api.nhtsa.gov/recalls/recallsByVehicle?make={make}&modelYear={year}"
            try:
                data = http_get_json(url)
            except Exception:
                continue
            for r in (data.get("results") or [])[:30]:
                cid = r.get("NHTSACampaignNumber")
                if not cid:
                    continue
                title = (r.get("Component") or "") + " — " + (r.get("Summary") or "")[:120]
                date_raw = r.get("ReportReceivedDate") or ""
                # NHTSA returns "DD/MM/YYYY"
                date = ""
                if "/" in date_raw:
                    parts = date_raw.split("/")
                    if len(parts) == 3:
                        date = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
                out.append({
                    "id": f"nhtsa-{cid}",
                    "source": "NHTSA",
                    "country": "US",
                    "category": "Vehicles",
                    "hazard": r.get("Component", "")[:80],
                    "severity": "high",
                    "date": date,
                    "title": f"{make} {year}: {r.get('Component', '')}".strip(": ").strip(),
                    "summary": (r.get("Summary") or "")[:400].strip(),
                    "url": f"https://www.nhtsa.gov/recalls?nhtsaId={cid}",
                })
    # Deduplicate by id
    seen = set()
    unique = []
    for item in out:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Source: USDA FSIS (food)
# ---------------------------------------------------------------------------
def fetch_fsis():
    url = "https://www.fsis.usda.gov/fsis/api/recall/v/1"
    data = http_get_json(url)
    out = []
    for r in data[:200]:
        rid = r.get("recallNumber") or r.get("field_recall_number")
        if not rid:
            continue
        title = r.get("field_title") or r.get("title") or "(untitled FSIS recall)"
        date = r.get("field_recall_date") or r.get("field_last_modified_date") or ""
        date = date[:10] if date else ""
        reason = r.get("field_recall_reason") or ""
        classn = r.get("field_recall_classification") or ""
        sev = "high" if "I" in classn[:3] else "medium"
        out.append({
            "id": f"fsis-{rid}",
            "source": "USDA FSIS",
            "country": "US",
            "category": "Food",
            "hazard": reason,
            "severity": sev,
            "date": date,
            "title": title,
            "summary": (r.get("field_summary") or r.get("field_product_items") or "")[:400].strip(),
            "url": r.get("field_url") or "https://www.fsis.usda.gov/recalls",
        })
    return out


# ---------------------------------------------------------------------------
# Source: FDA Food Enforcement (openFDA)
# ---------------------------------------------------------------------------
def fetch_fda_food():
    url = "https://api.fda.gov/food/enforcement.json?sort=report_date:desc&limit=100"
    data = http_get_json(url)
    out = []
    for r in (data.get("results") or [])[:100]:
        rid = r.get("recall_number")
        if not rid:
            continue
        date_raw = r.get("report_date") or ""
        date = ""
        if len(date_raw) == 8:  # YYYYMMDD
            date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        classn = r.get("classification") or ""
        sev_map = {"Class I": "high", "Class II": "medium", "Class III": "low"}
        sev = sev_map.get(classn, "medium")
        out.append({
            "id": f"fda-food-{rid}",
            "source": "FDA Food",
            "country": r.get("country") or "US",
            "category": "Food / Cosmetics",
            "hazard": (r.get("reason_for_recall") or "")[:120],
            "severity": sev,
            "date": date,
            "title": (r.get("product_description") or "(untitled)")[:200],
            "summary": (r.get("reason_for_recall") or "")[:400].strip(),
            "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
        })
    return out


# ---------------------------------------------------------------------------
# Source: EU Safety Gate (RAPEX)
# ---------------------------------------------------------------------------
def fetch_eu_safety_gate():
    """
    The EU Safety Gate publishes a weekly report. There's no clean public JSON
    API; we use the RSS feed from the public alerts site.
    """
    url = "https://ec.europa.eu/safety-gate-alerts/public/api/alerts/feed/rss"
    try:
        raw = http_get(url)
        root = ET.fromstring(raw)
        ns = ""  # default namespace handling
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            # Parse RFC 822 date roughly
            date = ""
            try:
                from email.utils import parsedate_to_datetime
                d = parsedate_to_datetime(pub)
                date = d.date().isoformat()
            except Exception:
                pass
            items.append({
                "id": f"eu-{abs(hash(link)) % (10**10)}",
                "source": "EU Safety Gate",
                "country": "EU",
                "category": "Consumer Product",
                "hazard": "",
                "severity": "medium",
                "date": date,
                "title": title[:200],
                "summary": desc[:400],
                "url": link,
            })
        return items[:150]
    except Exception:
        # Fallback: empty so we keep previous data
        raise


# ---------------------------------------------------------------------------
# Source: FTC Consumer Alerts (RSS)
# ---------------------------------------------------------------------------
def fetch_ftc():
    url = "https://consumer.ftc.gov/consumer-alerts/feed"
    raw = http_get(url)
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        date = ""
        try:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(pub)
            date = d.date().isoformat()
        except Exception:
            pass
        # Strip HTML tags crudely from description
        import re
        desc_clean = re.sub(r"<[^>]+>", "", desc)[:400]
        out.append({
            "id": f"ftc-{abs(hash(link)) % (10**10)}",
            "source": "FTC",
            "country": "US",
            "category": "Fraud & Scams",
            "hazard": "Financial",
            "severity": "medium",
            "date": date,
            "title": title[:200],
            "summary": desc_clean,
            "url": link,
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
            # Fall back to previous data for this source
            fallback = previous.get(label, [])
            all_items.extend(fallback)
            statuses[label] = "stale" if fallback else "missing"
            print(f"  [{label}] using {len(fallback)} cached items (status: {statuses[label]})")

    # Sort newest first
    all_items.sort(key=lambda x: x.get("date", ""), reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": "1.0",
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
