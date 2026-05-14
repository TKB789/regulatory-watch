#!/usr/bin/env python3
"""
build_consumer_feeds.py  (v5 — targeted fixes per Actions log)
--------------------------------------------------------------
Changes from v4 based on log errors:

  - NHTSA: HTTP 400 was because the Socrata $select column "summary" doesn't
    exist; the real column is "defect_summary". Fix: drop the $select clause
    entirely so we get all columns by default, then read the right ones.

  - USDA FSIS: even with a browser UA the JSON endpoint returns 403 from
    GitHub Actions runner IPs (FSIS API likely behind Akamai with deeper bot
    detection). Workaround: switch to the FSIS RSS feed used by recalls.gov,
    which is intended for syndication and remains accessible.

  - EU Safety Gate: the listing XML was downloaded fine but my parser found
    no numeric report IDs — meaning the tag names I guessed don't match
    what's actually in the list response. Fix: dump the discovered tag set
    on first run so we can see the real schema, and extract any digit-only
    text from elements at the right depth as a fallback.

Sources:
  - CPSC           https://www.saferproducts.gov/RestWebServices/Recall
  - NHTSA          https://data.transportation.gov/resource/6axg-epim.json
  - USDA FSIS      https://www.recalls.gov/rrusda.aspx   (RSS aggregator)
  - FDA Food       https://api.fda.gov/food/enforcement.json
  - EU Safety Gate https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/...
  - FTC            https://www.consumer.ftc.gov/blog/gd-rss.xml

Failure policy unchanged.
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
# Source: NHTSA (FIX: drop $select; real column is defect_summary, not summary)
# ---------------------------------------------------------------------------
def fetch_nhtsa():
    base = "https://data.transportation.gov/resource/6axg-epim.json"
    # Real columns per the live CSV header:
    #   report_received_date, nhtsa_id, recall_link, manufacturer, subject,
    #   component, mfr_campaign_number, recall_type, potentially_affected,
    #   defect_summary, consequence_summary, corrective_action,
    #   fire_risk_when_parked, do_not_drive, completion_rate
    params = {
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
# Source: USDA FSIS (FIX: switched to recalls.gov RSS aggregator)
# ---------------------------------------------------------------------------
def fetch_fsis():
    """The fsis.usda.gov JSON API is blocked by Akamai for GitHub Actions IPs.
    Use the recalls.gov aggregator's USDA RSS feed instead — same data, more
    permissive access. Fallback to the FSIS legacy RSS if recalls.gov is down.
    """
    urls = [
        "https://www.recalls.gov/rrusda.aspx",
        "https://www.fsis.usda.gov/RSS/usdarss.xml",
    ]
    raw = None
    for u in urls:
        try:
            raw = http_get(u)
            print(f"  [USDA FSIS] using source: {u}")
            break
        except Exception as e:
            print(f"  [USDA FSIS] tried {u}: {e}", file=sys.stderr)
    if raw is None:
        raise RuntimeError("all FSIS RSS sources failed")

    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        date = parse_rss_date(pub)
        # heuristic severity: "Public Health Alert" or "Class I" → high
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
# Source: EU Safety Gate (FIX: dump listing schema + fallback ID extraction)
# ---------------------------------------------------------------------------
EU_BASE = "https://ec.europa.eu/safety-gate-alerts/api/download"
EU_LIST_URL = f"{EU_BASE}/weeklyReport/list/xml/en"
EU_DETAIL_TEMPLATE = (EU_BASE + "/weeklyReport/detail/xml/{rid}"
                      "?language=en&search=WEB_REPORT%7C:%7C{rid}")
EU_WEEKS_TO_FETCH = 6


def eu_severity(risk_text):
    rt = (risk_text or "").lower()
    if "serious" in rt:
        return "high"
    return "medium"


def fetch_eu_safety_gate():
    list_xml = http_get(EU_LIST_URL)
    list_root = ET.fromstring(list_xml)
    strip_namespaces(list_root)

    # DIAGNOSTIC: log the tag structure of the listing so we can see what's
    # actually inside. Printed once per run.
    tag_counts = {}
    for el in list_root.iter():
        if isinstance(el.tag, str):
            tag_counts[el.tag] = tag_counts.get(el.tag, 0) + 1
    # Top 20 tags by frequency
    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:20]
    print(f"  [EU Safety Gate] listing tag frequencies (top 20): {top_tags}")

    # NEW STRATEGY: rather than guessing tag names, find every digit-only
    # text node in the document and pick the highest values as report IDs.
    # Weekly report IDs are large (>1000); year numbers like 2026 are too
    # small to be confused with them in practice (real IDs are 5–8 digits).
    candidate_ids = set()
    for el in list_root.iter():
        if isinstance(el.tag, str):
            txt = (el.text or "").strip()
            # 4-8 digit pure numbers that look like report ids
            if txt.isdigit():
                n = int(txt)
                if 1000 <= n <= 99999999:  # exclude 2-digit week numbers and years like 2026
                    # filter out year-shaped numbers
                    if not (1990 <= n <= 2099):
                        candidate_ids.add(n)

    candidate_ids = sorted(candidate_ids, reverse=True)
    if not candidate_ids:
        # Last resort: also try attribute values
        for el in list_root.iter():
            for v in (el.attrib or {}).values():
                if isinstance(v, str) and v.isdigit():
                    n = int(v)
                    if 1000 <= n <= 99999999 and not (1990 <= n <= 2099):
                        candidate_ids.add(n)
        candidate_ids = sorted(candidate_ids, reverse=True)

    if not candidate_ids:
        raise RuntimeError("EU listing parsed but no numeric report IDs found "
                           "(see logged tag frequencies above)")

    recent_ids = candidate_ids[:EU_WEEKS_TO_FETCH]
    print(f"  [EU Safety Gate] {len(candidate_ids)} candidate IDs, "
          f"using top {len(recent_ids)}: {recent_ids}")

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
            print(f"  [EU Safety Gate] parse error on {rid}: {e}", file=sys.stderr)
            continue

        strip_namespaces(root)

        alert_elements = []
        for cand in ("notification", "alert", "rapexnotification",
                     "weeklyreportnotification", "report", "alertdetail"):
            alert_elements = list(root.iter(cand))
            if len(alert_elements) > 1:
                break

        if not logged_schema and alert_elements:
            sample = alert_elements[0]
            tags_in_sample = sorted({el.tag for el in sample.iter() if isinstance(el.tag, str)})
            print(f"  [EU Safety Gate] container = <{sample.tag}>, "
                  f"sample inner tags = {tags_in_sample[:40]}")
            logged_schema = True
        elif not logged_schema:
            # No expected container found — dump top tags of the detail page too
            detail_tags = sorted({el.tag for el in root.iter() if isinstance(el.tag, str)})[:40]
            print(f"  [EU Safety Gate] no known container found in {rid}; "
                  f"detail tags = {detail_tags}")

        for alert in alert_elements:
            alert_num = first_text(alert, *ALERT_NUM_TAGS)
            title = first_text(alert, *TITLE_TAGS) or "(EU Safety Gate alert)"
            category = first_text(alert, *CATEGORY_TAGS) or "Consumer Product"
            risk = first_text(alert, *RISK_TAGS)
            country = first_text(alert, *COUNTRY_TAGS) or "EU"
            origin = first_text(alert, *ORIGIN_TAGS)
            date_raw = first_text(alert, *DATE_TAGS)
            description = first_text(alert, *DESCRIPTION_TAGS)

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
        "schema_version": "1.4",
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
