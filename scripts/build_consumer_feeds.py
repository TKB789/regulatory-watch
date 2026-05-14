#!/usr/bin/env python3
"""
build_consumer_feeds.py  (v11 — real EU field names)
----------------------------------------------------
The v10 dump revealed the actual field names inside each EU alert.
v11 uses them directly:

  Field name              | What we use it for
  ------------------------|--------------------------------------
  product                 | Product name (was using <name> — usually empty)
  brand                   | Brand (often empty, used when present)
  type_numberofmodel      | Model number suffix in title
  category                | Product category
  risktype                | One-word hazard ("Burns", "Choking", "Chemical")
  danger                  | Full hazard description (THE key text)
  description             | Physical product description
  measures                | Action taken (withdrawal, recall, ban, etc.)
  level                   | "Serious risk" / "Other risk" → severity bucket
  notifyingcountry        | Reporting EU country
  countryoforigin         | Where product was made (often China)
  casenumber              | Alert reference ID (e.g. SR/01326/26)
  reference               | Direct URL to the alert detail page
  barcode                 | Product barcode (for shopper verification)

Display:
  title:   "{caseNumber}: {brand — }{product}{(model)}"
  summary: hazard description · origin · action · barcode
  url:     uses the <reference> URL directly (no construction needed)

Diagnostic dump removed now that the schema is settled; we just log a
single sample line per run so we can spot future schema changes.
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

    # v11 — confirmed field names from real Safety Gate weekly XML:
    #   barcode, batchnumber, brand, casenumber, category, companyrecallcode,
    #   countryoforigin, danger, description, level, measures, name,
    #   notifyingcountry, order, pictures, product, productiondates,
    #   reference, risktype, type, type_numberofmodel, urlrecall

    def get_field(alert_dict, *names):
        for n in names:
            v = alert_dict.get(n.lower(), "")
            if v:
                return v
        return ""

    def eu_level_severity(level_text):
        """Map the EU 'level' field to our severity buckets."""
        lt = (level_text or "").lower()
        if "serious" in lt:
            return "high"
        if "other" in lt:
            return "low"
        return "medium"

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

        # The XML structure inside <notifications> is FLAT — not nested per
        # alert. Fields appear as siblings, delimited by <order> tags.
        alerts = []
        for notif_block in root.iter("notifications"):
            current = None
            for child in list(notif_block):
                if not isinstance(child.tag, str):
                    continue
                tag = child.tag.lower()
                text = (child.text or "").strip()
                if tag == "order":
                    if current is not None:
                        alerts.append(current)
                    current = {"order": text}
                else:
                    if current is None:
                        current = {}
                    if tag in current and current[tag]:
                        current[tag] = current[tag] + " | " + text if text else current[tag]
                    else:
                        current[tag] = text
            if current is not None:
                alerts.append(current)

        if not alerts:
            continue

        if not logged_schema:
            print(f"\n  [EU Safety Gate] v11 parsing — {len(alerts)} alerts in report {rid}")
            # One-line summary of the first alert so we can confirm fields look right
            a = alerts[0]
            print(f"    sample alert: product={a.get('product','')[:40]!r} | "
                  f"risktype={a.get('risktype','')!r} | "
                  f"level={a.get('level','')!r} | "
                  f"notifyingcountry={a.get('notifyingcountry','')!r} | "
                  f"countryoforigin={a.get('countryoforigin','')[:30]!r}")
            logged_schema = True

        for alert in alerts:
            # === EXTRACT FIELDS using the real EU schema ===
            case_number = get_field(alert, "casenumber")
            product = get_field(alert, "product")
            brand = get_field(alert, "brand")
            model = get_field(alert, "type_numberofmodel")
            category = get_field(alert, "category") or "Consumer Product"
            risktype = get_field(alert, "risktype")        # one-word hazard
            danger = get_field(alert, "danger")            # full hazard description
            description = get_field(alert, "description")  # physical product description
            measures = get_field(alert, "measures")
            level = get_field(alert, "level")              # "Serious risk", "Other risk", etc.
            notifying = get_field(alert, "notifyingcountry")
            origin = get_field(alert, "countryoforigin")
            barcode = get_field(alert, "barcode")
            reference_url = get_field(alert, "reference")  # already a complete URL!

            # === BUILD DISPLAY TITLE ===
            # Real product names live in <product>. Brand is usually empty.
            # Fall back order: product → brand → name → generic
            title_main = product or brand or get_field(alert, "name") or "(unnamed)"
            if brand and brand.lower() not in title_main.lower():
                title_main = f"{brand} — {title_main}"
            if model and model.lower() not in title_main.lower():
                title_main = f"{title_main} ({model})"
            display_title = (f"{case_number}: {title_main}" if case_number else title_main)[:200]

            # === BUILD SUMMARY ===
            # Lead with the hazard description, then product description, then origin.
            summary_bits = []
            if danger:
                summary_bits.append(danger)
            elif description:
                summary_bits.append(description)
            else:
                # Use both if neither was prioritized
                if description:
                    summary_bits.append(description)
            if origin:
                summary_bits.append(f"Origin: {origin}")
            if measures:
                # Strip the long "Type of economic operator..." preamble
                m = measures
                if "Category of measure(s):" in m:
                    m = m.split("Category of measure(s):", 1)[1].strip()
                summary_bits.append(f"Action: {m[:120]}")
            if barcode:
                summary_bits.append(f"Barcode: {barcode}")
            summary = " · ".join(summary_bits)[:500]

            # === COUNTRY / HAZARD / SEVERITY ===
            country_display = notifying or "EU"
            hazard_display = risktype or ""
            severity = eu_level_severity(level)

            # === URL: prefer the EU's own <reference> URL ===
            item_url = reference_url
            if not item_url:
                item_url = (f"https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/{rid}"
                            if rid else "https://ec.europa.eu/safety-gate-alerts/")

            out.append({
                "id": stable_id("eu", case_number, product, rid),
                "source": "EU Safety Gate",
                "country": country_display[:60],
                "category": category[:60],
                "hazard": hazard_display[:80],
                "severity": severity,
                "date": d.isoformat(),
                "title": display_title,
                "summary": summary,
                "url": item_url,
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
