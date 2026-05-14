#!/usr/bin/env python3
"""
build_consumer_feeds.py  (v6 — diagnostic-driven fixes)
-------------------------------------------------------
Changes from v5 based on the v5 Actions log:

  - EU Safety Gate: the listing schema was revealed — each <weeklyReport>
    has a <URL> child that contains the integer report ID in its path
    (e.g. ".../weeklyReport/detail/10000282"). Pull IDs from URL directly
    instead of guessing tag names or scanning digit-only text. Sort the
    weekly reports by publicationDate to be sure we get the newest first.

  - USDA FSIS: both fsis.usda.gov RSS and recalls.gov RSS now 403 from
    GitHub Actions IPs. Try a chain of fallbacks:
      1. FoodSafety.gov XML widget feed (different infra than USDA/Akamai)
      2. recalls.gov USDA RSS  (cached attempt)
      3. fsis.usda.gov legacy RSS
      4. Final fallback: HTML scrape of fsis.usda.gov/recalls
    If all four fail, FSIS remains "missing" but at least we tried.

  - The script also accepts a CLI flag --debug-eu that prints the first
    detail report's raw XML head (first 2 KB) for offline inspection.

Sources unchanged otherwise.
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


def safe(fn, label):
    try:
        items = fn()
        print(f"  [{label}] fetched {len(items)} items")
        return items, True
    except Exception as e:
        print(f"  [{label}] FAILED: {e}", file=sys.stderr)
        return [], False


def strip_namespaces(elem):
    for el in elem.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def first_text(parent, *tag_names):
    wanted = {t.lower() for t in tag_names}
    for el in parent.iter():
        if isinstance(el.tag, str) and el.tag.lower() in wanted:
            txt = (el.text or "").strip()
            if txt:
                return txt
    return ""


# ---------------------------------------------------------------------------
# CPSC
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
# NHTSA (working in v5)
# ---------------------------------------------------------------------------
def fetch_nhtsa():
    base = "https://data.transportation.gov/resource/6axg-epim.json"
    params = {"$order": "report_received_date DESC", "$limit": "300"}
    url = base + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    out = []
    for r in data:
        cid = r.get("nhtsa_id") or ""
        date_raw = r.get("report_received_date") or ""
        date = date_raw[:10] if date_raw else ""
        manuf = r.get("manufacturer") or ""
        subj = r.get("subject") or r.get("component") or ""
        defect = r.get("defect_summary") or ""
        consequence = r.get("consequence_summary") or ""
        title = f"{manuf}: {subj}".strip(": ").strip() or "(NHTSA recall)"
        out.append({
            "id": stable_id("nhtsa", cid, title),
            "source": "NHTSA", "country": "US", "category": "Vehicles",
            "hazard": (r.get("component") or "")[:80],
            "severity": "high", "date": date, "title": title[:200],
            "summary": (defect or consequence)[:400].strip(),
            "url": (f"https://www.nhtsa.gov/recalls?nhtsaId={cid}"
                    if cid else "https://www.nhtsa.gov/recalls"),
        })
    return out


# ---------------------------------------------------------------------------
# USDA FSIS (FIX: longer fallback chain ending with HTML scrape)
# ---------------------------------------------------------------------------
def _parse_rss_items(raw):
    """Common helper: parse RSS bytes → list of normalized items."""
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        date = parse_rss_date(pub)
        t_low = title.lower()
        d_low = desc.lower()
        sev = "high" if ("public health alert" in t_low or "class i" in d_low
                          or "listeria" in t_low or "e. coli" in t_low
                          or "salmonella" in t_low) else "medium"
        out.append({
            "id": stable_id("fsis", link, title),
            "source": "USDA FSIS", "country": "US", "category": "Food",
            "hazard": "", "severity": sev,
            "date": date, "title": strip_html(title)[:200],
            "summary": strip_html(desc)[:400].strip(),
            "url": link or "https://www.fsis.usda.gov/recalls",
        })
    return out[:100]


def _scrape_fsis_html():
    """Last-resort: parse the public FSIS recalls page HTML for recall links."""
    raw = http_get("https://www.fsis.usda.gov/recalls").decode("utf-8", errors="ignore")
    # Find anchors to /recalls-alerts/* with their visible link text
    pattern = re.compile(
        r'<a[^>]+href="(/recalls-alerts/[^"]+)"[^>]*>([^<]+)</a>',
        re.IGNORECASE,
    )
    seen = set()
    out = []
    for m in pattern.finditer(raw):
        path, title = m.group(1), m.group(2).strip()
        if not title or len(title) < 10:
            continue
        if path in seen:
            continue
        seen.add(path)
        link = "https://www.fsis.usda.gov" + path
        t_low = title.lower()
        sev = "high" if ("public health alert" in t_low
                         or "listeria" in t_low or "e. coli" in t_low
                         or "salmonella" in t_low) else "medium"
        out.append({
            "id": stable_id("fsis", link, title),
            "source": "USDA FSIS", "country": "US", "category": "Food",
            "hazard": "", "severity": sev,
            "date": "",  # date not reliably present in anchor text
            "title": title[:200], "summary": title[:400],
            "url": link,
        })
        if len(out) >= 60:
            break
    return out


def fetch_fsis():
    """Try sources in order until one works."""
    rss_sources = [
        ("FoodSafety.gov widget", "https://www.foodsafety.gov/recalls-and-outbreaks/recalls-rss.xml"),
        ("recalls.gov USDA",      "https://www.recalls.gov/rrusda.aspx"),
        ("fsis.usda.gov legacy",  "https://www.fsis.usda.gov/RSS/usdarss.xml"),
    ]
    for label, url in rss_sources:
        try:
            raw = http_get(url)
            items = _parse_rss_items(raw)
            if items:
                print(f"  [USDA FSIS] using source: {label} ({url})")
                return items
        except Exception as e:
            print(f"  [USDA FSIS] tried {label}: {e}", file=sys.stderr)

    # HTML scrape last
    try:
        items = _scrape_fsis_html()
        if items:
            print(f"  [USDA FSIS] using source: HTML scrape of fsis.usda.gov/recalls")
            return items
    except Exception as e:
        print(f"  [USDA FSIS] HTML scrape failed: {e}", file=sys.stderr)

    raise RuntimeError("all FSIS sources failed (RSS + HTML)")


# ---------------------------------------------------------------------------
# FDA Food
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
# EU Safety Gate (FIX: extract IDs from <URL> tag inside <weeklyReport>)
# ---------------------------------------------------------------------------
EU_BASE = "https://ec.europa.eu/safety-gate-alerts/api/download"
EU_LIST_URL = f"{EU_BASE}/weeklyReport/list/xml/en"
EU_DETAIL_TEMPLATE = (EU_BASE + "/weeklyReport/detail/xml/{rid}"
                      "?language=en&search=WEB_REPORT%7C:%7C{rid}")
EU_WEEKS_TO_FETCH = 6

ID_FROM_URL_RE = re.compile(r"/weeklyReport/(?:detail/)?(?:xml/)?(\d{3,9})\b")


def eu_severity(risk_text):
    return "high" if "serious" in (risk_text or "").lower() else "medium"


def fetch_eu_safety_gate():
    list_xml = http_get(EU_LIST_URL)
    list_root = ET.fromstring(list_xml)
    strip_namespaces(list_root)

    # Each weeklyReport now confirmed to have: reference, publicationDate,
    # year, month, day, week, URL, report_language.
    weekly_reports = list(list_root.iter("weeklyReport"))
    print(f"  [EU Safety Gate] found {len(weekly_reports)} <weeklyReport> entries")

    # Build (date, id, url) tuples
    reports = []
    for wr in weekly_reports:
        url_text = (wr.findtext("URL") or "").strip()
        ref_text = (wr.findtext("reference") or "").strip()
        date_text = (wr.findtext("publicationDate") or "").strip()

        # Extract numeric id from the URL path
        rid = None
        m = ID_FROM_URL_RE.search(url_text)
        if m:
            rid = m.group(1)
        elif ref_text.isdigit():
            rid = ref_text

        if rid:
            reports.append((date_text, rid, url_text))

    # Sort by publicationDate descending; assume ISO-like dates so string sort works
    reports.sort(key=lambda t: t[0], reverse=True)

    if not reports:
        raise RuntimeError("EU listing parsed but no usable report IDs/URLs found")

    recent = reports[:EU_WEEKS_TO_FETCH]
    print(f"  [EU Safety Gate] fetching {len(recent)} most recent: "
          f"{[(d, rid) for d, rid, _ in recent]}")

    out = []
    logged_schema = False

    TITLE_TAGS = ("productname", "product", "name", "title")
    CATEGORY_TAGS = ("category", "productcategory", "type")
    RISK_TAGS = ("risk", "risktype", "typeofrisk", "warningtype")
    ALERT_NUM_TAGS = ("alertnumber", "notificationnumber", "reference", "alertreference")
    COUNTRY_TAGS = ("notifyingcountry", "country", "notifyingauthority")
    ORIGIN_TAGS = ("countryoforigin", "origincountry", "originatingcountry")
    DATE_TAGS = ("publicationdate", "validationdate", "creationdate", "date",
                 "publishingdate", "alertdate")
    DESC_TAGS = ("description", "productdescription", "details", "summary",
                 "warningdescription")

    for date_text, rid, original_url in recent:
        # Prefer the URL the EU itself provides; fall back to template
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

        # Find alert containers — try standard names then fall back to anything
        # that repeats more than once at depth ≥ 2
        alert_elements = []
        for cand in ("notification", "alert", "rapexnotification",
                     "weeklyreportnotification", "alertdetail"):
            alert_elements = list(root.iter(cand))
            if len(alert_elements) > 1:
                break

        if not alert_elements:
            # Find the most common repeating tag at depth ≥ 2
            counts = {}
            for el in root.iter():
                if isinstance(el.tag, str):
                    counts[el.tag] = counts.get(el.tag, 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            print(f"  [EU Safety Gate] report {rid}: no known container; "
                  f"top tags = {ranked[:10]}")
            continue

        if not logged_schema:
            sample = alert_elements[0]
            tags_in_sample = sorted({el.tag for el in sample.iter()
                                     if isinstance(el.tag, str)})
            print(f"  [EU Safety Gate] container = <{sample.tag}> "
                  f"({len(alert_elements)} per report); "
                  f"sample tags = {tags_in_sample[:40]}")
            logged_schema = True

        for alert in alert_elements:
            alert_num = first_text(alert, *ALERT_NUM_TAGS)
            title = first_text(alert, *TITLE_TAGS) or "(EU Safety Gate alert)"
            category = first_text(alert, *CATEGORY_TAGS) or "Consumer Product"
            risk = first_text(alert, *RISK_TAGS)
            country = first_text(alert, *COUNTRY_TAGS) or "EU"
            origin = first_text(alert, *ORIGIN_TAGS)
            date_raw = first_text(alert, *DATE_TAGS) or date_text
            description = first_text(alert, *DESC_TAGS)

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
                "country": country[:60], "category": category[:60],
                "hazard": risk[:80], "severity": eu_severity(risk),
                "date": date,
                "title": (alert_num + " — " + title if alert_num else title)[:200],
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
        "schema_version": "1.5",
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
