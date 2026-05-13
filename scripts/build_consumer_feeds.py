#!/usr/bin/env python3
"""
build_consumer_feeds.py  (v4 — re-adds EU Safety Gate via weekly XML reports)
-----------------------------------------------------------------------------
Fetches consumer-safety data from multiple government sources, normalizes each
item to a common schema, and writes feeds/consumer.json.

What's new in v4:
  - EU Safety Gate is back. Approach: fetch the master list of weekly-report
    IDs from /api/download/weeklyReport/list/xml/en, take the most recent 6
    weeks (≈1 month of EU alerts), download each weekly XML report, and parse
    every alert/notification element inside. Field extraction is defensive —
    we walk the tree and match on a set of likely tag names rather than
    assume a single rigid schema. On the very first successful run we also
    log the discovered tag set so the schema is recorded in the Action log.

Sources:
  - CPSC          https://www.saferproducts.gov/RestWebServices/Recall
  - NHTSA         https://data.transportation.gov/resource/6axg-epim.json
  - USDA FSIS     https://www.fsis.usda.gov/fsis/api/recall/v/1
  - FDA Food      https://api.fda.gov/food/enforcement.json
  - EU Safety Gate https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/...
  - FTC           https://www.consumer.ftc.gov/blog/gd-rss.xml

Failure policy: each source fails independently. If one fails, we keep its
previous data; the others still update.
"""

import json
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
TIMEOUT = 30

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def http_get(url, headers=None):
    """Fetch a URL and return bytes. Raises on error."""
    base_headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        base_headers.update(headers)
    req = urllib.request.Request(url, headers=base_headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def http_get_json(url, headers=None):
    return json.loads(http_get(url, headers))


def stable_id(prefix, *parts):
    key = "|".join(str(p) for p in parts if p)
    if not key:
        key = str(datetime.now().timestamp())
    return f"{prefix}-{abs(hash(key)) % (10**12)}"


def strip_html(text):
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text)


def parse_rss_date(s):
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


def safe(fn, label):
    try:
        items = fn()
        print(f"  [{label}] fetched {len(items)} items")
        return items, True
    except Exception as e:
        print(f"  [{label}] FAILED: {e}", file=sys.stderr)
        return [], False


# Strip XML namespaces from an ElementTree (makes iter() with bare tag names work)
def strip_namespaces(elem):
    for el in elem.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def first_text(parent, *tag_names):
    """Find the first descendant matching any of tag_names (case-insensitive) and
    return its text, stripped. Returns '' if nothing matches."""
    wanted = {t.lower() for t in tag_names}
    for el in parent.iter():
        if isinstance(el.tag, str) and el.tag.lower() in wanted:
            txt = (el.text or "").strip()
            if txt:
                return txt
    return ""


# ---------------------------------------------------------------------------
# Source: CPSC
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
            "source": "CPSC", "country": "US", "category": cat,
            "hazard": hazard_text, "severity": "high",
            "date": date, "title": title,
            "summary": desc[:400].strip(), "url": url_field,
        })
    return out


# ---------------------------------------------------------------------------
# Source: NHTSA — Socrata bulk
# ---------------------------------------------------------------------------
def fetch_nhtsa():
    base = "https://data.transportation.gov/resource/6axg-epim.json"
    params = {
        "$select": "nhtsa_id,report_received_date,manufacturer,subject,component,summary,consequence_summary",
        "$order": "report_received_date DESC",
        "$limit": "300",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    out = []
    for r in data:
        cid = r.get("nhtsa_id") or ""
        date_raw = r.get("report_received_date") or ""
        date = date_raw[:10] if date_raw else ""
        manuf = r.get("manufacturer") or ""
        subj = r.get("subject") or r.get("component") or ""
        summary = r.get("summary") or ""
        consequence = r.get("consequence_summary") or ""
        title = f"{manuf}: {subj}".strip(": ").strip() or "(NHTSA recall)"
        out.append({
            "id": stable_id("nhtsa", cid, title),
            "source": "NHTSA", "country": "US", "category": "Vehicles",
            "hazard": (r.get("component") or "")[:80],
            "severity": "high", "date": date, "title": title[:200],
            "summary": (summary or consequence)[:400].strip(),
            "url": (f"https://www.nhtsa.gov/recalls?nhtsaId={cid}"
                    if cid else "https://www.nhtsa.gov/recalls"),
        })
    return out


# ---------------------------------------------------------------------------
# Source: USDA FSIS
# ---------------------------------------------------------------------------
def fetch_fsis():
    url = "https://www.fsis.usda.gov/fsis/api/recall/v/1"
    data = http_get_json(url)
    out = []
    for r in data[:200]:
        title = r.get("field_title") or r.get("title") or "(untitled FSIS recall)"
        rid = r.get("field_recall_number") or r.get("field_recall_url") or title[:50]
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
            "source": "USDA FSIS", "country": "US", "category": "Food",
            "hazard": strip_html(reason)[:120], "severity": sev,
            "date": date, "title": strip_html(title)[:200],
            "summary": strip_html(summary)[:400].strip(), "url": url_field,
        })
    return out


# ---------------------------------------------------------------------------
# Source: FDA Food
# ---------------------------------------------------------------------------
def fetch_fda_food():
    url = "https://api.fda.gov/food/enforcement.json?sort=report_date:desc&limit=100"
    data = http_get_json(url)
    out = []
    for r in (data.get("results") or [])[:100]:
        rid = r.get("recall_number")
        date_raw = r.get("report_date") or ""
        date = ""
        if len(date_raw) == 8:
            date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        classn = r.get("classification") or ""
        sev_map = {"Class I": "high", "Class II": "medium", "Class III": "low"}
        sev = sev_map.get(classn, "medium")
        title = (r.get("product_description") or "(untitled)")[:200]
        out.append({
            "id": stable_id("fda-food", rid, title),
            "source": "FDA Food", "country": r.get("country") or "US",
            "category": "Food / Cosmetics",
            "hazard": (r.get("reason_for_recall") or "")[:120],
            "severity": sev, "date": date, "title": title,
            "summary": (r.get("reason_for_recall") or "")[:400].strip(),
            "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
        })
    return out


# ---------------------------------------------------------------------------
# Source: EU Safety Gate (new in v4)
# ---------------------------------------------------------------------------
EU_BASE = "https://ec.europa.eu/safety-gate-alerts/api/download"
EU_LIST_URL = f"{EU_BASE}/weeklyReport/list/xml/en"
EU_DETAIL_TEMPLATE = (EU_BASE + "/weeklyReport/detail/xml/{rid}"
                      "?language=en&search=WEB_REPORT%7C:%7C{rid}")
EU_WEEKS_TO_FETCH = 6  # ~6 most recent weekly reports = roughly a month of alerts

# Heuristic mapping of EU risk text → severity bucket. Most Safety Gate alerts
# are "Serious risk" by definition; we keep the rest at "medium".
def eu_severity(risk_text):
    rt = (risk_text or "").lower()
    if "serious" in rt:
        return "high"
    if rt:
        return "medium"
    return "medium"


def fetch_eu_safety_gate():
    """Walk the weekly-report listing → take latest N report IDs → fetch and
    parse each. Defensive about tag names since the schema isn't documented."""

    # 1. Get the list of all weekly report IDs.
    list_xml = http_get(EU_LIST_URL)
    list_root = ET.fromstring(list_xml)
    strip_namespaces(list_root)

    # Each listing entry usually has a numeric reportNumber / id / number tag.
    # Collect every plausible integer that looks like a report id.
    candidate_ids = []
    for el in list_root.iter():
        if isinstance(el.tag, str) and el.tag.lower() in {
            "reportnumber", "reportid", "id", "number", "weeklyreportnumber"
        }:
            txt = (el.text or "").strip()
            if txt.isdigit():
                candidate_ids.append(int(txt))

    # Dedupe + sort descending (newest first). Real IDs are large 5–8 digit ints.
    candidate_ids = sorted(set(i for i in candidate_ids if i > 100), reverse=True)
    if not candidate_ids:
        raise RuntimeError("EU listing parsed but no numeric report IDs found")

    recent_ids = candidate_ids[:EU_WEEKS_TO_FETCH]
    print(f"  [EU Safety Gate] discovered {len(candidate_ids)} reports, "
          f"fetching {len(recent_ids)}: {recent_ids}")

    # 2. For each report, fetch the detail XML and parse alerts.
    out = []
    logged_schema = False  # log discovered field names once

    # Tag aliases — multiple guesses for each field, lowercase
    TITLE_TAGS = ("productname", "product", "name", "title")
    CATEGORY_TAGS = ("category", "productcategory", "type")
    RISK_TAGS = ("risk", "risktype", "typeofrisk", "warningtype")
    ALERT_NUM_TAGS = ("alertnumber", "notificationnumber", "reference", "alertreference")
    COUNTRY_TAGS = ("notifyingcountry", "country", "notifyingauthority")
    ORIGIN_TAGS = ("countryoforigin", "origincountry", "originatingcountry")
    DATE_TAGS = ("publicationdate", "validationdate", "creationdate", "date",
                 "publishingdate", "alertdate")
    DESCRIPTION_TAGS = ("description", "productdescription", "details", "summary",
                        "warningdescription")

    for rid in recent_ids:
        url = EU_DETAIL_TEMPLATE.format(rid=rid)
        try:
            xml = http_get(url)
        except Exception as e:
            print(f"  [EU Safety Gate] skipping report {rid}: {e}", file=sys.stderr)
            continue

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            print(f"  [EU Safety Gate] parse error on report {rid}: {e}", file=sys.stderr)
            continue

        strip_namespaces(root)

        # Look for top-level alert/notification containers. Try several names.
        alert_elements = []
        for cand in ("notification", "alert", "rapexnotification",
                     "weeklyreportnotification", "report", "alertdetail"):
            alert_elements = list(root.iter(cand))
            if len(alert_elements) > 1:  # >1 means we found the repeating container
                break

        # Log schema on first hit so we can see what fields are available
        if not logged_schema and alert_elements:
            sample = alert_elements[0]
            tags_in_sample = sorted({el.tag for el in sample.iter() if isinstance(el.tag, str)})
            print(f"  [EU Safety Gate] container = <{sample.tag}>, "
                  f"sample inner tags = {tags_in_sample[:40]}")
            logged_schema = True

        for alert in alert_elements:
            alert_num = first_text(alert, *ALERT_NUM_TAGS)
            title = first_text(alert, *TITLE_TAGS) or "(EU Safety Gate alert)"
            category = first_text(alert, *CATEGORY_TAGS) or "Consumer Product"
            risk = first_text(alert, *RISK_TAGS)
            country = first_text(alert, *COUNTRY_TAGS) or "EU"
            origin = first_text(alert, *ORIGIN_TAGS)
            date_raw = first_text(alert, *DATE_TAGS)
            description = first_text(alert, *DESCRIPTION_TAGS)

            # Normalize date — try ISO, DD/MM/YYYY, DD-MM-YYYY
            date = ""
            if date_raw:
                if re.match(r"^\d{4}-\d{2}-\d{2}", date_raw):
                    date = date_raw[:10]
                else:
                    m = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})", date_raw)
                    if m:
                        date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

            summary_bits = []
            if description:
                summary_bits.append(description)
            if origin:
                summary_bits.append(f"Origin: {origin}")
            summary = ". ".join(summary_bits)[:400]

            out.append({
                "id": stable_id("eu", rid, alert_num, title),
                "source": "EU Safety Gate",
                "country": country[:60],
                "category": category[:60],
                "hazard": risk[:80],
                "severity": eu_severity(risk),
                "date": date,
                "title": (alert_num + " — " + title if alert_num else title)[:200],
                "summary": summary,
                "url": (f"https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/{rid}"
                        if rid else "https://ec.europa.eu/safety-gate-alerts/"),
            })

    return out


# ---------------------------------------------------------------------------
# Source: FTC
# ---------------------------------------------------------------------------
def fetch_ftc():
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
            "source": "FTC", "country": "US",
            "category": "Fraud & Scams", "hazard": "Financial",
            "severity": "medium", "date": date, "title": title[:200],
            "summary": desc_clean,
            "url": link or "https://consumer.ftc.gov/consumer-alerts",
        })
    return out[:100]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    print("Building consumer.json…")

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
        "schema_version": "1.3",
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
