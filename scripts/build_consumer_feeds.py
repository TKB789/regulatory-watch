#!/usr/bin/env python3
"""
build_consumer_feeds.py  (v9 — adds full EU alert XML dump for diagnosis)
------------------------------------------------------------------------
v9 keeps every v8 behavior (6-month cap, all working sources) and adds
ONE diagnostic: on the first EU weekly report processed, it dumps the
full XML of the first 2 alerts to the Actions log, plus a field
inventory listing every tag with a sample value. This lets us see
which fields are actually populated for current EU alerts so v10 can
surface the right data.

To find the dump in the Actions log, search for:
    === RAW ALERT XML DUMP ===

No behavioral changes — just diagnostics. Safe to deploy.
"""

import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "feeds" / "consumer.json"
TIMEOUT = 30

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# 6 months ≈ 183 days. Items older than this cutoff are dropped.
SIX_MONTHS_AGO = (datetime.now(timezone.utc).date() - timedelta(days=183))


def http_get(url, headers=None):
    base = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        base.update(headers)
    req = urllib.request.Request(url, headers=base)
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


def parse_eu_date(s):
    """DD/MM/YYYY or ISO → date object or None."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        try:
            return date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except ValueError:
            return None
    return None


def within_six_months(iso_date_str):
    """True if iso_date_str (YYYY-MM-DD) is within last 6 months."""
    if not iso_date_str or len(iso_date_str) < 10:
        return False
    try:
        d = date.fromisoformat(iso_date_str[:10])
    except ValueError:
        return False
    return d >= SIX_MONTHS_AGO


def apply_six_month_cap(items, label):
    """Filter items to last 6 months. Logs before/after counts."""
    before = len(items)
    dated = [it for it in items if within_six_months(it.get("date", ""))]
    if dated:
        kept = dated
    else:
        # Source has no parseable dates at all — keep undated items so we
        # don't accidentally wipe a working source over a format quirk.
        kept = items
        print(f"  [{label}] no items had parseable dates; keeping all {before}", file=sys.stderr)
    after = len(kept)
    if before != after:
        print(f"  [{label}] 6-month cap: {before} → {after}")
    return kept


def safe(fn, label):
    try:
        items = fn()
        items = apply_six_month_cap(items, label)
        print(f"  [{label}] fetched {len(items)} items")
        return items, True
    except Exception as e:
        print(f"  [{label}] FAILED: {e}", file=sys.stderr)
        return [], False


def strip_namespaces(elem):
    for el in elem.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


# ---------------------------------------------------------------------------
# CPSC
# ---------------------------------------------------------------------------
def fetch_cpsc():
    url = "https://www.saferproducts.gov/RestWebServices/Recall?format=json"
    data = http_get_json(url)
    out = []
    for r in data[:400]:  # raised cap; will be filtered to 6 months after
        rid = r.get("RecallID") or r.get("RecallNumber")
        title = r.get("Title") or "(untitled CPSC recall)"
        date_raw = r.get("RecallDate") or ""
        d = date_raw[:10] if date_raw else ""
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
            "date": d, "title": title,
            "summary": desc[:400].strip(), "url": url_field,
        })
    return out


# ---------------------------------------------------------------------------
# NHTSA
# ---------------------------------------------------------------------------
def fetch_nhtsa():
    base = "https://data.transportation.gov/resource/6axg-epim.json"
    params = {"$order": "report_received_date DESC", "$limit": "500"}
    url = base + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    out = []
    for r in data:
        cid = r.get("nhtsa_id") or ""
        date_raw = r.get("report_received_date") or ""
        d = date_raw[:10] if date_raw else ""
        manuf = r.get("manufacturer") or ""
        subj = r.get("subject") or r.get("component") or ""
        defect = r.get("defect_summary") or ""
        consequence = r.get("consequence_summary") or ""
        title = f"{manuf}: {subj}".strip(": ").strip() or "(NHTSA recall)"
        out.append({
            "id": stable_id("nhtsa", cid, title),
            "source": "NHTSA", "country": "US", "category": "Vehicles",
            "hazard": (r.get("component") or "")[:80],
            "severity": "high", "date": d, "title": title[:200],
            "summary": (defect or consequence)[:400].strip(),
            "url": (f"https://www.nhtsa.gov/recalls?nhtsaId={cid}"
                    if cid else "https://www.nhtsa.gov/recalls"),
        })
    return out


# ---------------------------------------------------------------------------
# FDA Food
# ---------------------------------------------------------------------------
def fetch_fda_food():
    url = "https://api.fda.gov/food/enforcement.json?sort=report_date:desc&limit=200"
    data = http_get_json(url)
    out = []
    for r in (data.get("results") or [])[:200]:
        rid = r.get("recall_number")
        date_raw = r.get("report_date") or ""
        d = ""
        if len(date_raw) == 8:
            d = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        classn = r.get("classification") or ""
        sev_map = {"Class I": "high", "Class II": "medium", "Class III": "low"}
        sev = sev_map.get(classn, "medium")
        title = (r.get("product_description") or "(untitled)")[:200]
        out.append({
            "id": stable_id("fda-food", rid, title),
            "source": "FDA Food", "country": r.get("country") or "US",
            "category": "Food / Cosmetics",
            "hazard": (r.get("reason_for_recall") or "")[:120],
            "severity": sev, "date": d, "title": title,
            "summary": (r.get("reason_for_recall") or "")[:400].strip(),
            "url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
        })
    return out


# ---------------------------------------------------------------------------
# EU Safety Gate (still uses 6 most recent reports; cap then filters further)
# ---------------------------------------------------------------------------
EU_BASE = "https://ec.europa.eu/safety-gate-alerts/api/download"
EU_LIST_URL = f"{EU_BASE}/weeklyReport/list/xml/en"
EU_DETAIL_TEMPLATE = (EU_BASE + "/weeklyReport/detail/xml/{rid}"
                      "?language=en&search=WEB_REPORT%7C:%7C{rid}")
# Bumped from 6 to 26 weeks so we have full 6 months of EU data BEFORE the
# 6-month filter runs. Each report is small over the wire (~150 KB) so 26
# requests is still well under a minute total.
EU_WEEKS_TO_FETCH = 26

ID_FROM_URL_RE = re.compile(r"/(\d{3,9})(?:[/?#]|$)")


def eu_severity(risk_text):
    return "high" if "serious" in (risk_text or "").lower() else "medium"


def fetch_eu_safety_gate():
    list_xml = http_get(EU_LIST_URL)
    list_root = ET.fromstring(list_xml)
    strip_namespaces(list_root)

    weekly_reports = list(list_root.iter("weeklyReport"))
    print(f"  [EU Safety Gate] found {len(weekly_reports)} <weeklyReport> entries")

    reports = []
    for wr in weekly_reports:
        url_text = (wr.findtext("URL") or "").strip()
        ref_text = (wr.findtext("reference") or "").strip()
        pub = (wr.findtext("publicationDate") or "").strip()
        year = (wr.findtext("year") or "").strip()
        month = (wr.findtext("month") or "").strip()
        day = (wr.findtext("day") or "").strip()

        d = None
        if year.isdigit() and month.isdigit() and day.isdigit():
            try:
                d = date(int(year), int(month), int(day))
            except ValueError:
                d = None
        if d is None:
            d = parse_eu_date(pub)
        if d is None:
            d = date(1900, 1, 1)

        rid = None
        m = ID_FROM_URL_RE.search(url_text)
        if m:
            rid = m.group(1)
        elif ref_text.isdigit():
            rid = ref_text

        if rid:
            reports.append((d, rid, url_text))

    # Sort newest first AND filter listing to last 6 months upfront — saves
    # us from downloading older weekly reports we'll only discard later.
    reports = [r for r in reports if r[0] >= SIX_MONTHS_AGO]
    reports.sort(key=lambda t: t[0], reverse=True)
    if not reports:
        raise RuntimeError("no EU weekly reports within last 6 months")

    recent = reports[:EU_WEEKS_TO_FETCH]
    print(f"  [EU Safety Gate] downloading {len(recent)} weekly reports "
          f"(date range {recent[-1][0].isoformat()} → {recent[0][0].isoformat()})")

    out = []
    logged_schema = False

    TITLE_TAGS = ("name", "product", "productname", "title")
    BRAND_TAGS = ("brand",)
    CATEGORY_TAGS = ("category", "productcategory")
    RISK_TAGS = ("risk", "risk_description", "riskdescription",
                 "risktype", "typeofrisk")
    ALERT_NUM_TAGS = ("casenumber", "reference", "alertnumber",
                      "notificationnumber")
    COUNTRY_TAGS = ("notifyingcountry", "country", "notifyingauthority")
    ORIGIN_TAGS = ("countryoforigin", "origincountry", "originatingcountry",
                   "origin")
    DESC_TAGS = ("description", "productdescription", "details",
                 "warningdescription", "measures_description",
                 "measuresdescription")
    MODEL_TAGS = ("type_numberofmodel", "type", "model")

    def first_text_of(parent, *tag_names):
        wanted = {t.lower() for t in tag_names}
        for el in parent.iter():
            if isinstance(el.tag, str) and el.tag.lower() in wanted:
                txt = (el.text or "").strip()
                if txt:
                    return txt
        return ""

    for d, rid, original_url in recent:
        detail_url = original_url or EU_DETAIL_TEMPLATE.format(rid=rid)
        try:
            xml = http_get(detail_url)
        except Exception as e:
            print(f"  [EU Safety Gate] skipping report {rid}: {e}", file=sys.stderr)
            continue

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            print(f"  [EU Safety Gate] parse error on {rid}: {e}", file=sys.stderr)
            continue

        strip_namespaces(root)

        alerts = []
        for notif_block in root.iter("notifications"):
            for child in list(notif_block):
                if isinstance(child.tag, str):
                    alerts.append(child)

        if not alerts:
            continue

        if not logged_schema:
            print(f"\n  [EU Safety Gate] === RAW ALERT XML DUMP ===")
            print(f"  Report ID: {rid}    Date: {d.isoformat()}    Alerts in report: {len(alerts)}")
            print(f"  Showing first 2 alerts verbatim so we can see every field and its real content.\n")

            for idx, sample in enumerate(alerts[:2]):
                print(f"  --- ALERT #{idx + 1} ---")
                # Serialize the alert subtree back to a string so every field
                # and its value is visible. Pretty-print at depth.
                raw = ET.tostring(sample, encoding="unicode")
                # Compress runs of whitespace so the log is readable, but keep
                # tag boundaries intact.
                raw = re.sub(r"\n\s*", "\n", raw).strip()
                # Truncate each alert to ~3000 chars max for log readability
                if len(raw) > 3000:
                    raw = raw[:3000] + "\n  ...[truncated]..."
                # Indent each line by 4 spaces so it reads nicely in the log
                for line in raw.split("\n"):
                    print(f"    {line}")
                print()

            # Also print a structural summary: every tag with content + a sample value
            print(f"  --- FIELD INVENTORY (across first alert) ---")
            sample = alerts[0]
            seen_fields = {}
            for el in sample.iter():
                if not isinstance(el.tag, str) or el.tag == sample.tag:
                    continue
                if el.tag not in seen_fields:
                    val = (el.text or "").strip()
                    val_display = val[:80] if val else "(empty)"
                    seen_fields[el.tag] = val_display
            for tag, val in sorted(seen_fields.items()):
                print(f"    <{tag}>{val}</{tag}>")
            print(f"  === END DUMP ===\n")
            logged_schema = True

        for alert in alerts:
            alert_num = first_text_of(alert, *ALERT_NUM_TAGS)
            product_name = first_text_of(alert, *TITLE_TAGS) or "(unnamed product)"
            brand = first_text_of(alert, *BRAND_TAGS)
            category = first_text_of(alert, *CATEGORY_TAGS) or "Consumer Product"
            risk = first_text_of(alert, *RISK_TAGS)
            country = first_text_of(alert, *COUNTRY_TAGS) or "EU"
            origin = first_text_of(alert, *ORIGIN_TAGS)
            description = first_text_of(alert, *DESC_TAGS)
            model = first_text_of(alert, *MODEL_TAGS)

            display_title = product_name
            if brand and brand.lower() not in product_name.lower():
                display_title = f"{brand} {product_name}"
            if alert_num:
                display_title = f"{alert_num} — {display_title}"

            summary_bits = []
            if description:
                summary_bits.append(description)
            if model:
                summary_bits.append(f"Model: {model}")
            if origin:
                summary_bits.append(f"Origin: {origin}")
            summary = " · ".join(summary_bits)[:400]

            out.append({
                "id": stable_id("eu", rid, alert_num, product_name),
                "source": "EU Safety Gate",
                "country": country[:60],
                "category": category[:60],
                "hazard": risk[:80],
                "severity": eu_severity(risk),
                "date": d.isoformat(),
                "title": display_title[:200],
                "summary": summary,
                "url": (f"https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/{rid}"
                        if rid else "https://ec.europa.eu/safety-gate-alerts/"),
            })

    return out


# ---------------------------------------------------------------------------
# FTC
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
        d = parse_rss_date(pub)
        desc_clean = strip_html(desc)[:400]
        out.append({
            "id": stable_id("ftc", link, title),
            "source": "FTC", "country": "US",
            "category": "Fraud & Scams", "hazard": "Financial",
            "severity": "medium", "date": d, "title": title[:200],
            "summary": desc_clean,
            "url": link or "https://consumer.ftc.gov/consumer-alerts",
        })
    return out[:100]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main():
    print(f"Building consumer.json… (6-month cutoff: {SIX_MONTHS_AGO.isoformat()})")

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
            # Apply 6-month cap to fallback data too
            fallback = [it for it in fallback if within_six_months(it.get("date", ""))]
            all_items.extend(fallback)
            statuses[label] = "stale" if fallback else "missing"
            print(f"  [{label}] using {len(fallback)} cached items (status: {statuses[label]})")

    statuses["USDA FSIS"] = "blocked_by_source"

    all_items.sort(key=lambda x: x.get("date", ""), reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": "1.7",
        "window_days": 183,
        "cutoff_date": SIX_MONTHS_AGO.isoformat(),
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
